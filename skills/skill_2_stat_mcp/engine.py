r"""Skill 2: 白名单受控统计引擎（`skill_2_stat_mcp.engine`）。

严格实现 5 个纯确定性白名单统计工具，严禁 `eval` / `exec` / `DataFrame.query` 或任意代码执行：
1. `inspect_data(dataset_id)`
2. `aggregate(dataset_id, target_col_id, metric, filters=None)`
3. `group_compare(dataset_id, target_col_id, group_col_id, metric, stratify_col_id=None)`
4. `sensitivity_check(dataset_id, target_col_id, group_col_id, method)`
5. `get_evidence(run_id, evidence_id=None)`

所有参数仅接受已登记的 `dataset_id`、`col_id` 与枚举值；缺失统计量严格返回 `None`（序列化为 `null`）；
每次执行按规范化 `request_key` 生成确定性 `EvidenceRecord` 并持久化至本次运行证据账本。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd
from scipy import stats
from scipy.stats.mstats import winsorize

from core.schema import (
    EvidenceRecord,
    canonical_json,
    compute_request_key,
)
from core.security import DatasetRegistry, SecurityError, resolve_safe_path, sandbox_root, validate_run_id
from skills.skill_1_data_profiler.skill import (
    REPO_ROOT,
    build_default_registry,
    compute_column_summaries,
)

ALLOWED_AGG_METRICS: Set[str] = {"mean", "median", "sum", "count", "rate"}
ALLOWED_COMPARE_METRICS: Set[str] = {"mean", "median"}
ALLOWED_SENSITIVITY_METHODS: Set[str] = {
    "winsorize_5pct",
    "trim_top_1pct",
    "median_vs_mean",
    "drop_outliers_iqr",
}

TOOL_VERSION = "1.0"


def _finite_numeric(series: pd.Series) -> np.ndarray:
    coerced = pd.to_numeric(series, errors="coerce")
    arr = coerced.to_numpy(dtype=float)
    return arr[np.isfinite(arr)]


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    f = float(val)
    if not np.isfinite(f):
        return None
    return round(f, 6)


def _compute_group_diff(
    treated: np.ndarray,
    control: np.ndarray,
    metric: str,
) -> Dict[str, Any]:
    if len(treated) == 0 or len(control) == 0:
        return {
            "treated_n": int(len(treated)),
            "control_n": int(len(control)),
            "treated_stat": None,
            "control_stat": None,
            "diff": None,
            "p_value": None,
            "ci_95_low": None,
            "ci_95_high": None,
        }

    if metric == "mean":
        t_stat = float(np.mean(treated))
        c_stat = float(np.mean(control))
    elif metric == "median":
        t_stat = float(np.median(treated))
        c_stat = float(np.median(control))
    else:
        raise ValueError(f"Unsupported compare metric: '{metric}'")

    diff = t_stat - c_stat
    p_value: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None

    if metric == "mean" and len(treated) >= 2 and len(control) >= 2:
        # Welch inference for the mean difference. Medians and pairs of
        # constant samples have no such standard error.
        vt = float(np.var(treated, ddof=1) / len(treated))
        vc = float(np.var(control, ddof=1) / len(control))
        variance = vt + vc
        if np.isfinite(variance) and variance > 0:
            se = float(np.sqrt(variance))
            df = 1 / ((vt / variance) ** 2 / (len(treated) - 1) + (vc / variance) ** 2 / (len(control) - 1))
            p_value = _safe_float(2 * stats.t.sf(abs(diff) / se, df))
            margin = float(stats.t.ppf(0.975, df) * se)
            ci_low = _safe_float(diff - margin)
            ci_high = _safe_float(diff + margin)

    return {
        "treated_n": int(len(treated)),
        "control_n": int(len(control)),
        "treated_stat": _safe_float(t_stat),
        "control_stat": _safe_float(c_stat),
        "diff": _safe_float(diff),
        "p_value": p_value,
        "ci_95_low": ci_low,
        "ci_95_high": ci_high,
    }


def _split_binary_groups(
    df: pd.DataFrame,
    target_col_id: str,
    group_col_id: str,
) -> Tuple[np.ndarray, np.ndarray, Any, Any]:
    valid_groups = df[group_col_id].dropna().unique().tolist()
    if len(valid_groups) != 2:
        raise ValueError(f"Group column '{group_col_id}' must contain exactly 2 distinct groups")

    # 优先使用 1 vs 0（发券组 vs 未发券组），否则按排序末组减首组
    if 1 in valid_groups and 0 in valid_groups:
        treated_key, control_key = 1, 0
    elif "1" in valid_groups and "0" in valid_groups:
        treated_key, control_key = "1", "0"
    else:
        sorted_vals = sorted(valid_groups, key=lambda x: str(x))
        control_key, treated_key = sorted_vals[0], sorted_vals[-1]

    treated = _finite_numeric(df.loc[df[group_col_id] == treated_key, target_col_id])
    control = _finite_numeric(df.loc[df[group_col_id] == control_key, target_col_id])
    return treated, control, treated_key, control_key


class StatAuditEngine:
    """受控白名单统计取证引擎与确定性证据账本。"""

    def __init__(
        self,
        *,
        registry: Optional[DatasetRegistry] = None,
        output_root: str | Path = REPO_ROOT / "workspace/outputs",
    ) -> None:
        self.registry = registry or build_default_registry()
        self.output_root = sandbox_root(output_root)
        self._ledgers: Dict[str, Dict[str, EvidenceRecord]] = {}
        self._request_cache: Dict[Tuple[str, str], str] = {}

    def register_dataset(self, file_path: str | Path, dataset_id: Optional[str] = None) -> Dict[str, Any]:
        profile = self.registry.register_csv(file_path, dataset_id=dataset_id)
        return profile.model_dump(mode="json")

    def _validate_col(self, dataset_id: str, col_id: str) -> None:
        self.registry.get_column_mapping(dataset_id, col_id)

    def _record_evidence(
        self,
        *,
        run_id: str,
        claim_id: str,
        dataset_hash: str,
        tool_id: str,
        parameters: Dict[str, Any],
        estimand: str,
        scope_id: str,
        sample_size: Optional[int],
        result: Dict[str, Any],
        limitations: Optional[List[str]] = None,
        assumption_checks: Optional[Dict[str, Any]] = None,
    ) -> EvidenceRecord:
        req_hash = compute_request_key(dataset_hash, tool_id, TOOL_VERSION, parameters)
        cache_key = (run_id, req_hash)
        if cache_key in self._request_cache:
            existing_id = self._request_cache[cache_key]
            existing = self._ledgers[run_id][existing_id]
            if existing.claim_id != claim_id:
                raise ValueError("Request already bound to a different claim")
            return existing

        res_hash = hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()
        ev_id = f"ev_{req_hash[:12]}"

        record = EvidenceRecord(
            evidence_id=ev_id,
            claim_id=claim_id,
            dataset_hash=dataset_hash,
            tool_id=tool_id,
            tool_version=TOOL_VERSION,
            execution_status="ok",
            estimand=estimand,
            scope_id=scope_id,
            sample_size=sample_size,
            result=result,
            applicable=True,
            assumption_checks=assumption_checks or {},
            limitations=limitations or [],
            request_hash=req_hash,
            result_hash=res_hash,
        )

        self._persist_record(run_id, record)
        self._ledgers.setdefault(run_id, {})[ev_id] = record
        self._request_cache[cache_key] = ev_id
        return record

    def _persist_record(self, run_id: str, record: EvidenceRecord) -> None:
        target_dir = sandbox_root(self.output_root / validate_run_id(run_id))
        target_dir.mkdir(parents=True, exist_ok=True)
        ledger_file = resolve_safe_path(target_dir / "evidence_ledger.jsonl", [target_dir])
        # A new engine never appends to an older or concurrently owned run.
        mode = "a" if run_id in self._ledgers else "x"
        with ledger_file.open(mode, encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")

    # ------------------------------------------------------------------
    # Tool 1: inspect_data
    # ------------------------------------------------------------------
    def inspect_data(
        self,
        dataset_id: str,
        *,
        run_id: str = "default_run",
        claim_id: str = "claim_01",
    ) -> EvidenceRecord:
        profile = self.registry.get_profile(dataset_id)
        df = self.registry.get_dataframe(dataset_id)
        summary = compute_column_summaries(df, profile)
        result = {
            "dataset_id": profile.dataset_id,
            "row_count": profile.row_count,
            "col_count": profile.col_count,
            "candidate_group_cols": profile.candidate_group_cols,
            "quality_warnings": profile.quality_warnings,
            "duplicate_rows": summary["duplicate_rows"],
            "column_summaries": summary["column_summaries"],
        }
        return self._record_evidence(
            run_id=run_id,
            claim_id=claim_id,
            dataset_hash=profile.dataset_hash,
            tool_id="inspect_data",
            parameters={"dataset_id": dataset_id},
            estimand="dataset_profile",
            scope_id="global",
            sample_size=profile.row_count,
            result=result,
            limitations=list(profile.quality_warnings),
        )

    # ------------------------------------------------------------------
    # Tool 2: aggregate
    # ------------------------------------------------------------------
    def aggregate(
        self,
        dataset_id: str,
        target_col_id: str,
        metric: str,
        filters: Optional[Dict[str, Any]] = None,
        *,
        run_id: str = "default_run",
        claim_id: str = "claim_01",
    ) -> EvidenceRecord:
        if metric not in ALLOWED_AGG_METRICS:
            raise ValueError(f"Unsupported aggregate metric '{metric}'. Allowed: {sorted(ALLOWED_AGG_METRICS)}")

        profile = self.registry.get_profile(dataset_id)
        self._validate_col(dataset_id, target_col_id)
        df = self.registry.get_dataframe(dataset_id)

        applied_filters: Dict[str, Any] = {}
        if filters:
            for col_id, expected_val in filters.items():
                self._validate_col(dataset_id, col_id)
                if not isinstance(expected_val, (str, bool, int, float)) or (
                    isinstance(expected_val, float) and not np.isfinite(expected_val)
                ):
                    raise ValueError("Filter values must be finite JSON scalars")
                df = df[df[col_id] == expected_val]
                applied_filters[col_id] = expected_val

        series = df[target_col_id]
        value: Optional[float]
        if metric == "count":
            value = float(series.dropna().shape[0])
        else:
            arr = _finite_numeric(series)
            if len(arr) == 0:
                value = None
            elif metric == "mean":
                value = _safe_float(np.mean(arr))
            elif metric == "median":
                value = _safe_float(np.median(arr))
            elif metric == "sum":
                value = _safe_float(np.sum(arr))
            elif metric == "rate":
                value = _safe_float(np.mean(arr > 0))
            else:
                value = None

        params = {
            "dataset_id": dataset_id,
            "target_col_id": target_col_id,
            "metric": metric,
            "filters": applied_filters or None,
        }
        return self._record_evidence(
            run_id=run_id,
            claim_id=claim_id,
            dataset_hash=profile.dataset_hash,
            tool_id="aggregate",
            parameters=params,
            estimand=f"{metric}({target_col_id})",
            scope_id="filtered" if applied_filters else "global",
            sample_size=int(len(df)),
            result={
                "target_col_id": target_col_id,
                "metric": metric,
                "value": value,
                "row_count": int(len(df)),
            },
        )

    # ------------------------------------------------------------------
    # Tool 3: group_compare (supports stratify_col_id for Simpson's paradox)
    # ------------------------------------------------------------------
    def group_compare(
        self,
        dataset_id: str,
        target_col_id: str,
        group_col_id: str,
        metric: str = "mean",
        stratify_col_id: Optional[str] = None,
        *,
        run_id: str = "default_run",
        claim_id: str = "claim_01",
    ) -> EvidenceRecord:
        if metric not in ALLOWED_COMPARE_METRICS:
            raise ValueError(
                f"Unsupported group_compare metric '{metric}'. Allowed: {sorted(ALLOWED_COMPARE_METRICS)}"
            )

        profile = self.registry.get_profile(dataset_id)
        self._validate_col(dataset_id, target_col_id)
        self._validate_col(dataset_id, group_col_id)
        if stratify_col_id is not None:
            self._validate_col(dataset_id, stratify_col_id)

        df = self.registry.get_dataframe(dataset_id)
        treated, control, t_label, c_label = _split_binary_groups(df, target_col_id, group_col_id)
        overall = _compute_group_diff(treated, control, metric)
        result: Dict[str, Any] = {
            "target_col_id": target_col_id,
            "group_col_id": group_col_id,
            "metric": metric,
            "treated_group": str(t_label),
            "control_group": str(c_label),
            **overall,
        }
        limitations: List[str] = []

        if stratify_col_id is not None:
            strata_results: List[Dict[str, Any]] = []
            for sval, sub_df in df.groupby(stratify_col_id, sort=True, dropna=True):
                sub_t = _finite_numeric(sub_df.loc[sub_df[group_col_id] == t_label, target_col_id])
                sub_c = _finite_numeric(sub_df.loc[sub_df[group_col_id] == c_label, target_col_id])
                sub_stats = _compute_group_diff(sub_t, sub_c, metric)
                strata_results.append({"stratum": str(sval), **sub_stats})
            result["stratify_col_id"] = stratify_col_id
            result["strata"] = strata_results

        params = {
            "dataset_id": dataset_id,
            "target_col_id": target_col_id,
            "group_col_id": group_col_id,
            "metric": metric,
            "stratify_col_id": stratify_col_id,
        }
        return self._record_evidence(
            run_id=run_id,
            claim_id=claim_id,
            dataset_hash=profile.dataset_hash,
            tool_id="group_compare",
            parameters=params,
            estimand=f"{metric}_diff({target_col_id}|{group_col_id})",
            scope_id=f"stratified:{stratify_col_id}" if stratify_col_id else "global",
            sample_size=int(len(treated) + len(control)),
            result=result,
            limitations=limitations,
        )

    # ------------------------------------------------------------------
    # Tool 4: sensitivity_check
    # ------------------------------------------------------------------
    def sensitivity_check(
        self,
        dataset_id: str,
        target_col_id: str,
        group_col_id: str,
        method: str = "winsorize_5pct",
        *,
        run_id: str = "default_run",
        claim_id: str = "claim_01",
    ) -> EvidenceRecord:
        if method not in ALLOWED_SENSITIVITY_METHODS:
            raise ValueError(
                f"Unsupported sensitivity method '{method}'. Allowed: {sorted(ALLOWED_SENSITIVITY_METHODS)}"
            )

        profile = self.registry.get_profile(dataset_id)
        self._validate_col(dataset_id, target_col_id)
        self._validate_col(dataset_id, group_col_id)
        df = self.registry.get_dataframe(dataset_id)

        treated, control, t_label, c_label = _split_binary_groups(df, target_col_id, group_col_id)
        baseline_diff = float(np.mean(treated) - np.mean(control)) if len(treated) and len(control) else None

        adjusted_diff: Optional[float] = None
        if baseline_diff is not None:
            if method == "winsorize_5pct":
                adj_t = np.asarray(winsorize(treated, limits=(0.05, 0.05)), dtype=float)
                adj_c = np.asarray(winsorize(control, limits=(0.05, 0.05)), dtype=float)
                adjusted_diff = float(np.mean(adj_t) - np.mean(adj_c))
            elif method == "trim_top_1pct":
                t_cap = float(np.percentile(treated, 99))
                c_cap = float(np.percentile(control, 99))
                adj_t = treated[treated <= t_cap]
                adj_c = control[control <= c_cap]
                adjusted_diff = float(np.mean(adj_t) - np.mean(adj_c)) if len(adj_t) and len(adj_c) else None
            elif method == "median_vs_mean":
                adjusted_diff = float(np.median(treated) - np.median(control))
            elif method == "drop_outliers_iqr":
                def _drop_iqr(arr: np.ndarray) -> np.ndarray:
                    q25, q75 = np.percentile(arr, [25, 75])
                    iqr = q75 - q25
                    return arr[(arr >= q25 - 1.5 * iqr) & (arr <= q75 + 1.5 * iqr)]

                adj_t = _drop_iqr(treated)
                adj_c = _drop_iqr(control)
                adjusted_diff = float(np.mean(adj_t) - np.mean(adj_c)) if len(adj_t) and len(adj_c) else None

        rel_change: Optional[float] = None
        if baseline_diff is not None and adjusted_diff is not None:
            rel_change = _safe_float(abs(adjusted_diff - baseline_diff) / max(abs(baseline_diff), 1e-9))

        limitations: List[str] = []
        if rel_change is not None and rel_change > 0.50:
            limitations.append(
                f"{method} 后差值从 {_safe_float(baseline_diff)} 变动至 {_safe_float(adjusted_diff)}（相对变化率 {rel_change:.1%} > 50%）。"
            )

        params = {
            "dataset_id": dataset_id,
            "target_col_id": target_col_id,
            "group_col_id": group_col_id,
            "method": method,
        }
        return self._record_evidence(
            run_id=run_id,
            claim_id=claim_id,
            dataset_hash=profile.dataset_hash,
            tool_id="sensitivity_check",
            parameters=params,
            estimand=f"sensitivity_{method}({target_col_id}|{group_col_id})",
            scope_id="global",
            sample_size=int(len(treated) + len(control)),
            result={
                "target_col_id": target_col_id,
                "group_col_id": group_col_id,
                "treated_group": str(t_label),
                "control_group": str(c_label),
                "method": method,
                "baseline_diff": _safe_float(baseline_diff),
                "adjusted_diff": _safe_float(adjusted_diff),
                "relative_change_ratio": rel_change,
            },
            limitations=limitations,
        )

    # ------------------------------------------------------------------
    # Tool 5: get_evidence
    # ------------------------------------------------------------------
    def get_evidence(
        self,
        run_id: str,
        evidence_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        ledger = self._ledgers.get(validate_run_id(run_id), {})
        if evidence_id is not None:
            if evidence_id not in ledger:
                raise SecurityError(f"Evidence ID '{evidence_id}' not found in run '{run_id}'")
            return [ledger[evidence_id].model_dump(mode="json")]
        return [ev.model_dump(mode="json") for ev in ledger.values()]
