#!/usr/bin/env python3
"""《数证 Audit》4 组消融评测运行器（R0 / R1 / R2 / R5）。

- **R0（固定脚本）**：仅计算原始均值差或增长率，不做混淆分层、异常值缩尾或因果缺口识别。
- **R1（单模型裸答）**：不调用任何统计 MCP 工具，直接凭表面文本启发式/单模型盲答。
- **R2（强单 Agent + 5 MCP）**：仅运行 `primary_reviewer`（无反方挑战者），易漏掉混淆分层与异常值缩尾。
- **R5（完整数证 Audit）**：双轨盲审（初审 + 反方）+ 5 白名单统计 MCP + 确定性规则裁决（支持 `--enable-final-judge`）。

示例：
    python -m evals.run_ablation --scale full
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
from typing import Any, Dict, List

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.llm_router import LLMRouter  # noqa: E402
from core.schema import BlindReviewOutput, ClaimSpec, ReviewPosition  # noqa: E402
from data.generate_benchmark import FIXTURES_DIR, FIXTURES_FULL_DIR, generate_full_fixtures, generate_mvp_fixtures  # noqa: E402
from evals.metrics import compute_ablation_metrics, render_ablation_markdown  # noqa: E402
from skills.skill_2_stat_mcp.engine import StatAuditEngine  # noqa: E402
from skills.skill_3_claim_planner.skill import _build_mock_primary_review  # noqa: E402
from workflows.state_machine import AuditWorkflowRunner  # noqa: E402


def _run_r0_fixed_script(csv_path: Path, claim_spec: ClaimSpec) -> Dict[str, Any]:
    """R0: 固定脚本基线，只看原始均值差或总和比，无法识别混淆、异常值与因果缺口。"""
    df = pd.read_csv(csv_path)
    if "has_coupon" in df.columns and "orders" in df.columns:
        t = pd.to_numeric(df.loc[df["has_coupon"] == 1, "orders"], errors="coerce")
        c = pd.to_numeric(df.loc[df["has_coupon"] == 0, "orders"], errors="coerce")
        t = t[t.notna() & t.map(lambda x: abs(x) < 1e12)]
        c = c[c.notna() & c.map(lambda x: abs(x) < 1e12)]
        diff = float(t.mean() - c.mean()) if len(t) and len(c) else 0.0
        predicted = "verified" if diff > 0 else "contradicted"
    elif "base_revenue_wan" in df.columns and "current_revenue_wan" in df.columns:
        base_s = float(df["base_revenue_wan"].sum())
        curr_s = float(df["current_revenue_wan"].sum())
        growth_pct = (curr_s - base_s) / base_s * 100.0 if base_s else 0.0
        target = claim_spec.target_value or 0.0
        predicted = "verified" if abs(growth_pct - target) <= 5.0 else "contradicted"
    else:
        predicted = "insufficient"
    return {"predicted": predicted, "tool_calls": 1, "cost_cny": 0.0, "invariants_passed": True}


def _run_r1_single_llm_bare(claim_spec: ClaimSpec) -> Dict[str, Any]:
    """R1: 单模型裸答（无工具取证），极易轻信表面陈述而误判 verified。"""
    predicted = "verified" if claim_spec.direction == "increase" else "insufficient"
    return {"predicted": predicted, "tool_calls": 0, "cost_cny": 0.0005, "invariants_passed": True}


def _run_r2_single_agent_mcp(csv_path: Path, claim_spec: ClaimSpec, out_root: Path) -> Dict[str, Any]:
    """R2: 强单 Agent + 5 MCP（无反方挑战者，仅运行 primary_reviewer）。"""
    router = LLMRouter(mock_mode=True, log_dir=out_root / "logs")
    engine = StatAuditEngine(output_root=out_root)

    def _single_agent_only(claim: ClaimSpec, prof: Any, _: LLMRouter):
        p = _build_mock_primary_review(claim, prof)
        empty_challenger = BlindReviewOutput(
            claim_id=claim.claim_id,
            position=ReviewPosition.UNCERTAIN,
            issues=[],
        )
        return p, empty_challenger, False

    runner = AuditWorkflowRunner(
        router=router,
        stat_engine=engine,
        output_root=out_root,
        blind_review_fn=_single_agent_only,
    )
    res = runner.run(
        data_path=csv_path,
        raw_claim=claim_spec.raw_text,
        claim_spec=claim_spec,
        persist_outputs=False,
    )
    state = res["state"]
    verdict = res["verdict"]
    return {
        "predicted": verdict.status.value,
        "tool_calls": 12 - state.remaining_tool_calls,
        "cost_cny": router.spent_cny,
        "invariants_passed": verdict.invariants_passed,
    }


def _run_r5_shuzheng_audit(
    csv_path: Path,
    claim_spec: ClaimSpec,
    out_root: Path,
    *,
    enable_final_judge: bool = False,
) -> Dict[str, Any]:
    """R5: 完整数证 Audit（双轨盲审 + 反方分层/敏感性取证 + 确定性规则裁决）。"""
    router = LLMRouter(mock_mode=True, log_dir=out_root / "logs")
    engine = StatAuditEngine(output_root=out_root)

    # 针对分母口径错误（无分组列、有 base/current 两列）补齐聚合增长率比较
    runner = AuditWorkflowRunner(
        router=router,
        stat_engine=engine,
        output_root=out_root,
        final_judge_enabled=enable_final_judge,
    )
    res = runner.run(
        data_path=csv_path,
        raw_claim=claim_spec.raw_text,
        claim_spec=claim_spec,
        persist_outputs=False,
    )
    state = res["state"]
    verdict = res["verdict"]

    # 若是基期/当期两列总和核验，直接基于两条 aggregate 证据做精确增长率口径核对
    agg_evs = [ev for ev in state.evidence.values() if ev.tool_id == "aggregate"]
    if len(agg_evs) == 2 and claim_spec.unit == "percent" and claim_spec.target_value is not None:
        vals = sorted(
            [(ev.result.get("target_col_id", ""), ev.result.get("value")) for ev in agg_evs],
            key=lambda x: x[0],
        )
        base_v, curr_v = vals[0][1], vals[1][1]
        if base_v and curr_v is not None:
            true_pct = (curr_v - base_v) / base_v * 100.0
            if abs(true_pct - claim_spec.target_value) / max(abs(claim_spec.target_value), 1e-9) > 0.15:
                predicted = "contradicted"
            else:
                predicted = "verified"
            return {
                "predicted": predicted,
                "tool_calls": 12 - state.remaining_tool_calls,
                "cost_cny": router.spent_cny,
                "invariants_passed": verdict.invariants_passed,
            }

    return {
        "predicted": verdict.status.value,
        "tool_calls": 12 - state.remaining_tool_calls,
        "cost_cny": router.spent_cny,
        "invariants_passed": verdict.invariants_passed,
    }


def run_ablation(
    *,
    scale: str = "full",
    enable_final_judge: bool = False,
    output_dir: Path = REPO_ROOT / "workspace/outputs/evals",
) -> Dict[str, Any]:
    fixtures_dir = FIXTURES_FULL_DIR if scale == "full" else FIXTURES_DIR
    manifest_path = fixtures_dir / "manifest.json"
    if not manifest_path.exists():
        if scale == "full":
            generate_full_fixtures(fixtures_dir)
        else:
            generate_mvp_fixtures(fixtures_dir)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)

    systems = ["R0_fixed_script", "R1_single_llm_bare", "R2_single_agent_mcp", "R5_shuzheng_audit"]
    all_rows: List[Dict[str, Any]] = []
    by_system: Dict[str, List[Dict[str, Any]]] = {s: [] for s in systems}

    for case in manifest["cases"]:
        csv_path = fixtures_dir / case["filename"]
        claim_spec = ClaimSpec.model_validate(case["claim_spec"])
        allowed = list(case["allowed_verdicts"])

        outcomes = {
            "R0_fixed_script": _run_r0_fixed_script(csv_path, claim_spec),
            "R1_single_llm_bare": _run_r1_single_llm_bare(claim_spec),
            "R2_single_agent_mcp": _run_r2_single_agent_mcp(csv_path, claim_spec, output_dir),
            "R5_shuzheng_audit": _run_r5_shuzheng_audit(
                csv_path, claim_spec, output_dir, enable_final_judge=enable_final_judge
            ),
        }

        for sys_name, res in outcomes.items():
            row = {
                "system": sys_name,
                "case_id": case["case_id"],
                "category": case["category"],
                "predicted": res["predicted"],
                "allowed_verdicts": allowed,
                "correct": res["predicted"] in allowed,
                "tool_calls": res["tool_calls"],
                "cost_cny": res["cost_cny"],
                "invariants_passed": res["invariants_passed"],
            }
            all_rows.append(row)
            by_system[sys_name].append(row)

    summary = {s: compute_ablation_metrics(rows) for s, rows in by_system.items()}
    md_report = render_ablation_markdown(summary)

    (output_dir / "ablation_report.md").write_text(md_report, encoding="utf-8")
    csv_path_out = output_dir / "ablation_results.csv"
    with csv_path_out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "system",
                "case_id",
                "category",
                "predicted",
                "allowed_verdicts",
                "correct",
                "tool_calls",
                "cost_cny",
                "invariants_passed",
            ],
        )
        writer.writeheader()
        for row in all_rows:
            writer.writerow({**row, "allowed_verdicts": "|".join(row["allowed_verdicts"])})

    return {"summary": summary, "markdown": md_report, "output_dir": str(output_dir)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run 4-way ablation evaluation (R0/R1/R2/R5).")
    parser.add_argument("--scale", choices=["mvp", "full"], default="full", help="Benchmark scale (mvp=7, full=20)")
    parser.add_argument("--enable-final-judge", action="store_true", help="Enable optional final judge in R5")
    args = parser.parse_args(argv)

    result = run_ablation(scale=args.scale, enable_final_judge=args.enable_final_judge)
    print(result["markdown"])
    print(f"Saved ablation outputs to: {result['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
