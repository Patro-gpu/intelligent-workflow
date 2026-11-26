"""Skill 1: 数据质量与语义体检（`skill_1_data_profiler`）。

调用 `core/security.py` 只读注册 CSV，输出冻结契约 `DatasetProfile` 及脱敏列级统计摘要
（缺失率、分位数、重复行、IQR 极值数量），严禁向外部暴露原始文件路径或未清洗恶意表头。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
import yaml

from core.schema import DatasetProfile
from core.security import DatasetRegistry

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MCP_CONFIG = REPO_ROOT / "configs/mcp_servers.yaml"


def load_sandbox_config(config_path: str | Path = DEFAULT_MCP_CONFIG) -> Dict[str, Any]:
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    return dict(cfg["servers"]["stat_mcp"]["sandbox"])


def build_default_registry(
    *,
    config_path: str | Path = DEFAULT_MCP_CONFIG,
    extra_allowed_roots: Optional[Sequence[str | Path]] = None,
) -> DatasetRegistry:
    sandbox = load_sandbox_config(config_path)
    roots: List[Path] = [REPO_ROOT / r for r in sandbox.get("allowed_read_roots", [])]
    if extra_allowed_roots:
        roots.extend(Path(r) for r in extra_allowed_roots)
    return DatasetRegistry(
        allowed_roots=roots,
        max_file_size_mb=float(sandbox.get("max_file_size_mb", 10.0)),
        max_rows=int(sandbox.get("max_rows", 100_000)),
        max_cols=int(sandbox.get("max_cols", 100)),
    )


def compute_column_summaries(df: pd.DataFrame, profile: DatasetProfile) -> Dict[str, Any]:
    """基于安全 `col_id` 计算缺失率、分位数、重复行与 IQR 极值摘要（缺失统计量返回 None）。"""
    duplicate_rows = int(df.duplicated().sum())
    col_stats: Dict[str, Dict[str, Any]] = {}

    for col in profile.columns:
        series = df[col.col_id]
        total = len(series)
        missing_count = int(series.isna().sum())
        missing_rate = round(missing_count / total, 6) if total > 0 else None

        stat: Dict[str, Any] = {
            "col_id": col.col_id,
            "sanitized_name": col.sanitized_name,
            "dtype": col.dtype,
            "role_hint": col.role_hint,
            "missing_count": missing_count,
            "missing_rate": missing_rate,
            "nunique": int(series.dropna().nunique()),
            "quantiles": None,
            "iqr_outlier_count": None,
        }

        if pd.api.types.is_numeric_dtype(series):
            finite = series.replace([np.inf, -np.inf], np.nan).dropna().astype(float)
            if len(finite) > 0:
                q25, q50, q75 = np.percentile(finite, [25, 50, 75])
                iqr = float(q75 - q25)
                lower = float(q25 - 1.5 * iqr)
                upper = float(q75 + 1.5 * iqr)
                outlier_count = int(((finite < lower) | (finite > upper)).sum())
                stat["quantiles"] = {
                    "min": round(float(finite.min()), 6),
                    "p25": round(float(q25), 6),
                    "p50": round(float(q50), 6),
                    "p75": round(float(q75), 6),
                    "max": round(float(finite.max()), 6),
                }
                stat["iqr_outlier_count"] = outlier_count

        col_stats[col.col_id] = stat

    return {
        "row_count": profile.row_count,
        "col_count": profile.col_count,
        "duplicate_rows": duplicate_rows,
        "column_summaries": col_stats,
    }


def inspect_csv(
    file_path: str | Path,
    *,
    dataset_id: Optional[str] = None,
    registry: Optional[DatasetRegistry] = None,
) -> Dict[str, Any]:
    """独立体检入口：只读注册 CSV 并返回 `DatasetProfile` 与脱敏质量摘要。"""
    active_registry = registry or build_default_registry()
    profile = active_registry.register_csv(file_path, dataset_id=dataset_id)
    df = active_registry.get_dataframe(profile.dataset_id)
    summary = compute_column_summaries(df, profile)
    return {
        "profile": profile.model_dump(mode="json"),
        "summary": summary,
    }
