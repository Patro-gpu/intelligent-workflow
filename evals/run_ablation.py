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
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any, Dict, List
from uuid import uuid4

import pandas as pd
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.llm_router import BudgetExceededError, LLMRouter, ModelCallError  # noqa: E402
from core.security import DatasetRegistry, resolve_safe_path, sandbox_root, sanitize_cell_for_export
from core.schema import BlindReviewOutput, ClaimSpec, DatasetProfile, ReviewPosition, StrictModel, VerdictStatus  # noqa: E402
from data.generate_benchmark import FIXTURES_DIR, FIXTURES_FULL_DIR  # noqa: E402
from evals.metrics import compute_ablation_metrics, render_ablation_markdown  # noqa: E402
from skills.skill_3_claim_planner.skill import run_reviewer  # noqa: E402
from workflows.state_machine import AuditWorkflowRunner  # noqa: E402


def _run_r0_fixed_script(csv_path: Path, claim_spec: ClaimSpec) -> Dict[str, Any]:
    """R0: 固定脚本基线，只看原始均值差或总和比，无法识别混淆、异常值与因果缺口。"""
    registry = DatasetRegistry([FIXTURES_DIR, FIXTURES_FULL_DIR])
    profile = registry.register_csv(csv_path)
    labels = {"orders", "has_coupon", "base_revenue_wan", "current_revenue_wan"}
    columns = {c.col_id: c.sanitized_name for c in profile.columns if c.sanitized_name in labels}
    df = registry.get_dataframe(profile.dataset_id).rename(columns=columns)
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
    return {"predicted": predicted, "tool_calls": 0, "cost_cny": 0.0, "invariants_passed": True}


class BarePrediction(StrictModel):
    predicted: VerdictStatus


def _run_r1_single_llm_bare(claim: ClaimSpec, router: LLMRouter) -> Dict[str, Any]:
    """Bare baseline. This prediction is only used in the comparison table."""
    start_cost = router.spent_cny
    response = router.complete(
        role="primary_reviewer",
        messages=[
            {"role": "system", "content": '仅输出一个 JSON 对象，唯一键 predicted。值取 verified、limited、'
                                          'contradicted、insufficient 之一。根据给定命题进行独立预测。'},
            {"role": "user", "content": claim.model_dump_json()},
        ],
        max_tokens=64,
        mock_payload={"predicted": "verified" if claim.direction == "increase" else "insufficient"},
    )
    predicted = BarePrediction.model_validate(response.parsed_json).predicted
    return {"predicted": predicted.value, "tool_calls": 0, "cost_cny": router.spent_cny - start_cost,
            "invariants_passed": True}


def _single_agent_only(
    claim: ClaimSpec, profile: DatasetProfile, router: LLMRouter,
) -> tuple[BlindReviewOutput, BlindReviewOutput, bool]:
    primary = run_reviewer("primary_reviewer", claim, profile, router)
    return primary, BlindReviewOutput(claim_id=claim.claim_id, position=ReviewPosition.UNCERTAIN), False


def _run_workflow(
    csv_path: Path, claim: ClaimSpec, out_root: Path, router: LLMRouter, *,
    single_agent: bool = False, enable_final_judge: bool = False,
) -> Dict[str, Any]:
    start_cost = router.spent_cny
    runner = AuditWorkflowRunner(
        router=router,
        output_root=out_root,
        blind_review_fn=_single_agent_only if single_agent else None,
        final_judge_enabled=enable_final_judge,
    )
    result = runner.run(data_path=csv_path, raw_claim=claim.raw_text, claim_spec=claim, persist_outputs=False)
    state, verdict = result["state"], result["verdict"]
    if not router.mock_mode and "BUDGET" in state.stop_reason:
        raise BudgetExceededError("Live ablation stopped at the shared experiment budget")
    if not router.mock_mode and state.stop_reason == "JUDGE_UNAVAILABLE":
        raise ModelCallError("Live ablation stopped because the configured final reviewer was unavailable")
    return {
        "predicted": verdict.status.value,
        "tool_calls": runner.max_tool_calls - state.remaining_tool_calls,
        "cost_cny": router.spent_cny - start_cost,
        "invariants_passed": verdict.invariants_passed,
        "model_diversity_lost": state.model_diversity_lost,
    }


def run_ablation(
    *,
    scale: str = "full",
    enable_final_judge: bool = False,
    output_dir: Path = REPO_ROOT / "workspace/outputs/evals",
    live: bool = False,
    budget_cny: float | None = None,
) -> Dict[str, Any]:
    fixtures_dir = FIXTURES_FULL_DIR if scale == "full" else FIXTURES_DIR
    manifest_path = resolve_safe_path(fixtures_dir / "manifest.json", [fixtures_dir])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output_dir = sandbox_root(output_dir)
    if live:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_dir = resolve_safe_path(output_dir / "live" / f"{stamp}_{uuid4().hex[:12]}", [output_dir])
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = resolve_safe_path(output_dir / "logs", [output_dir])
    live_router = None
    if live:
        # All systems and cases share one experiment ceiling. The configured task cap
        # also bounds each case because the experiment cannot exceed it.
        live_router = LLMRouter(mock_mode=False, log_dir=log_dir)
        if budget_cny is not None:
            if not math.isfinite(budget_cny) or budget_cny < 0:
                raise ValueError("Budget must be finite and non-negative")
            live_router.max_budget_cny = min(live_router.max_budget_cny, budget_cny)
        live_router.check_live_configuration(["primary_reviewer", "challenger"])

    def router_for_run() -> LLMRouter:
        return live_router or LLMRouter(mock_mode=True, log_dir=log_dir, max_budget_cny=budget_cny)

    systems = ["R0_fixed_script", "R1_single_llm_bare", "R2_single_agent_mcp", "R5_shuzheng_audit"]
    by_system: Dict[str, List[Dict[str, Any]]] = {s: [] for s in systems}
    fields = ["mode", "model_diversity_lost", "system", "case_id", "category", "predicted",
              "allowed_verdicts", "correct", "tool_calls", "cost_cny", "invariants_passed"]
    # A partially paid run remains inspectable without publishing aggregate metrics.
    partial_csv = resolve_safe_path(output_dir / "ablation_results.partial.csv", [output_dir])
    with partial_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for case in manifest["cases"]:
            csv_path = resolve_safe_path(fixtures_dir / case["filename"], [fixtures_dir])
            claim = ClaimSpec.model_validate(case["claim_spec"])
            allowed = list(case["allowed_verdicts"])
            for system in systems:
                try:
                    if system == "R0_fixed_script":
                        res = _run_r0_fixed_script(csv_path, claim)
                    elif system == "R1_single_llm_bare":
                        res = _run_r1_single_llm_bare(claim, router_for_run())
                    else:
                        single = system == "R2_single_agent_mcp"
                        res = _run_workflow(csv_path, claim, output_dir, router_for_run(), single_agent=single,
                                            enable_final_judge=enable_final_judge and not single)
                except (BudgetExceededError, ModelCallError) as exc:
                    raise type(exc)(f"{exc}. Completed rows: {partial_csv}") from None
                except ValidationError:
                    raise ModelCallError(f"Invalid response schema. Completed rows: {partial_csv}") from None
                row = {
                    **res, "mode": "live" if live else "mock",
                    "model_diversity_lost": res.get("model_diversity_lost", False),
                    "system": system, "case_id": case["case_id"], "category": case["category"],
                    "allowed_verdicts": allowed, "correct": res["predicted"] in allowed,
                }
                by_system[system].append(row)
                export_row = {**row, "allowed_verdicts": "|".join(allowed)}
                writer.writerow({k: sanitize_cell_for_export(v) for k, v in export_row.items()})
                fh.flush()

    summary = {s: compute_ablation_metrics(rows) for s, rows in by_system.items()}
    md_report = render_ablation_markdown(summary) + ("\nMode: live API\n" if live else "\nMode: offline mock\n")
    partial_csv.replace(resolve_safe_path(output_dir / "ablation_results.csv", [output_dir]))
    resolve_safe_path(output_dir / "ablation_report.md", [output_dir]).write_text(md_report, encoding="utf-8")
    return {"summary": summary, "markdown": md_report, "output_dir": str(output_dir)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run 4-way ablation evaluation (R0/R1/R2/R5).")
    parser.add_argument("--scale", choices=["mvp", "full"], default="full", help="Benchmark scale (mvp=7, full=20)")
    parser.add_argument("--enable-final-judge", action="store_true", help="Enable optional final judge in R5")
    parser.add_argument("--live", action="store_true", help="Use real API providers (default: offline mock)")
    parser.add_argument("--budget-cny", type=float, help="Lower the shared live experiment cap (MAX_TASK_BUDGET_CNY)")
    args = parser.parse_args(argv)

    try:
        result = run_ablation(scale=args.scale, enable_final_judge=args.enable_final_judge,
                              live=args.live, budget_cny=args.budget_cny)
    except (BudgetExceededError, ModelCallError) as exc:
        parser.exit(2, f"Evaluation stopped: {exc}\n")
    except ValidationError:
        parser.exit(2, "Evaluation stopped: model or fixture JSON failed contract validation.\n")
    print(result["markdown"])
    print(f"Saved ablation outputs to: {result['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
