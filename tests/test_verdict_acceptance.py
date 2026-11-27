"""《数证 Audit》必过验收测试 T01–T12 与 6 条全局强制不变量断言。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Tuple

import pandas as pd
import pytest

from core.llm_router import LLMRouter
from core.schema import (
    BlindReviewOutput,
    BlindedJudgeInput,
    ClaimSpec,
    ClaimType,
    CrossReviewDelta,
    DatasetProfile,
    IssueItem,
    ReviewAction,
    ReviewPosition,
    SubClaim,
    ToolRequest,
    VerdictStatus,
    WorkflowState,
)
from skills.skill_2_stat_mcp.engine import StatAuditEngine
from workflows.cli import main as audit_cli_main
from workflows.state_machine import AuditWorkflowRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "data/fixtures"


def _assert_6_invariants(state: WorkflowState) -> None:
    assert state.round <= 2
    assert state.judge_calls <= 1
    assert state.duplicate_tool_executions == 0
    assert state.invalid_evidence_accepted == 0
    assert state.judge_rule_overrides == 0
    assert state.tool_calls_after_terminal == 0


def test_t01_both_agree_and_required_checks_pass_round1_verified(tmp_path: Path) -> None:
    """T01: 双方一致且必要检验通过 -> 第 1 轮 verified。"""
    router = LLMRouter(mock_mode=True, log_dir=tmp_path)

    def _blind_agree(claim: ClaimSpec, prof: DatasetProfile, _: LLMRouter) -> Tuple[BlindReviewOutput, BlindReviewOutput, bool]:
        req = ToolRequest(
            tool_id="group_compare",
            parameters={"dataset_id": prof.dataset_id, "target_col_id": "col_04", "group_col_id": "col_02", "metric": "mean"},
        )
        p = BlindReviewOutput(
            claim_id=claim.claim_id,
            position=ReviewPosition.SUPPORT,
            issues=[IssueItem(issue_id="i1", issue_type="metric", target_claim_id=claim.claim_id, requested_test=req)],
        )
        c = BlindReviewOutput(
            claim_id=claim.claim_id,
            position=ReviewPosition.SUPPORT,
            issues=[IssueItem(issue_id="i2", issue_type="metric", target_claim_id=claim.claim_id, requested_test=req)],
        )
        return p, c, False

    runner = AuditWorkflowRunner(router=router, output_root=tmp_path, blind_review_fn=_blind_agree)
    res = runner.run(
        data_path=FIXTURES_DIR / "case_01_valid_claim.csv",
        raw_claim="发券组商户订单量高于未发券组。",
    )
    state = res["state"]
    assert state.status == VerdictStatus.VERIFIED
    assert state.round == 1
    _assert_6_invariants(state)


def test_t02_both_agree_but_mcp_decisive_refutation_overrides_consensus(tmp_path: Path) -> None:
    """T02: 双方一致支持但有效 MCP 存在决定性反证 -> contradicted（忽略模型共识）。"""
    router = LLMRouter(mock_mode=True, log_dir=tmp_path)

    def _blind_both_support(
        claim: ClaimSpec, prof: DatasetProfile, _: LLMRouter
    ) -> Tuple[BlindReviewOutput, BlindReviewOutput, bool]:
        req = ToolRequest(
            tool_id="group_compare",
            parameters={
                "dataset_id": prof.dataset_id,
                "target_col_id": "col_04",
                "group_col_id": "col_02",
                "metric": "mean",
                "stratify_col_id": "col_03",
            },
        )
        out = BlindReviewOutput(
            claim_id=claim.claim_id,
            position=ReviewPosition.SUPPORT,
            issues=[IssueItem(issue_id="i_strat", issue_type="confounding", target_claim_id=claim.claim_id, requested_test=req)],
        )
        return out, out, False

    runner = AuditWorkflowRunner(router=router, output_root=tmp_path, blind_review_fn=_blind_both_support)
    res = runner.run(
        data_path=FIXTURES_DIR / "case_03_promo_confounding.csv",
        raw_claim="发放消费券使商户订单增加了 48 单。",
    )
    state = res["state"]
    assert state.status == VerdictStatus.CONTRADICTED
    _assert_6_invariants(state)


def test_t03_winsorize_48_to_18_direction_claim_limited(tmp_path: Path) -> None:
    """T03: 原始 +48、缩尾后 +18（衰减 62.5%），方向命题且稳健性属审计要求 -> limited。"""
    router = LLMRouter(mock_mode=True, log_dir=tmp_path)
    runner = AuditWorkflowRunner(router=router, output_root=tmp_path)
    res = runner.run(
        data_path=FIXTURES_DIR / "case_04_outlier_sensitivity.csv",
        raw_claim="发券组商户订单量高于未发券组。",
    )
    state = res["state"]
    assert state.status == VerdictStatus.LIMITED
    _assert_6_invariants(state)


def test_t04_universal_positive_claim_negative_subgroup_counterexample_contradicted(tmp_path: Path) -> None:
    """T04: 全称正向命题存在可靠负向子组反例 -> contradicted。"""
    csv_path = tmp_path / "case_t04_subgroup_neg.csv"
    df = pd.DataFrame(
        {
            "merchant_id": [f"M_{i}" for i in range(40)],
            "has_coupon": [1] * 10 + [0] * 10 + [1] * 10 + [0] * 10,
            "region": ["north"] * 20 + ["south"] * 20,
            "orders": [140.0] * 10 + [100.0] * 10 + [85.0] * 10 + [100.0] * 10,
        }
    )
    df.to_csv(csv_path, index=False)

    from core.security import DatasetRegistry

    registry = DatasetRegistry(allowed_roots=[tmp_path, FIXTURES_DIR])
    engine = StatAuditEngine(registry=registry, output_root=tmp_path)
    router = LLMRouter(mock_mode=True, log_dir=tmp_path)
    runner = AuditWorkflowRunner(router=router, stat_engine=engine, output_root=tmp_path)

    res = runner.run(
        data_path=csv_path,
        raw_claim="所有区域发券组订单量均高于未发券组。",
    )
    state = res["state"]
    assert state.status == VerdictStatus.CONTRADICTED
    _assert_6_invariants(state)


def test_t05_round2_repetition_closes_debate_zero_duplicate_execution(tmp_path: Path) -> None:
    """T05: 第 2 轮双方仅复读 -> 不重复执行工具，关闭辩论。"""
    router = LLMRouter(mock_mode=True, log_dir=tmp_path)

    def _repeat_cross(_: ClaimSpec, state: WorkflowState) -> List[Tuple[str, CrossReviewDelta]]:
        # 复读第 1 轮已执行过的 group_compare 请求
        first_ev = next(iter(state.evidence.values()))
        return [
            (
                "primary_reviewer",
                CrossReviewDelta(
                    claim_id=state.claim_id,
                    action=ReviewAction.ADD_NEW_TEST,
                    requested_test=ToolRequest(
                        tool_id=first_ev.tool_id,
                        parameters={
                            "dataset_id": first_ev.result.get("dataset_id", ""),
                            "target_col_id": first_ev.result.get("target_col_id", "col_04"),
                            "group_col_id": first_ev.result.get("group_col_id", "col_02"),
                            "metric": first_ev.result.get("metric", "mean"),
                        },
                    ),
                    rationale="重复相同请求",
                ),
            ),
            (
                "challenger",
                CrossReviewDelta(
                    claim_id=state.claim_id,
                    action=ReviewAction.NO_NEW_EVIDENCE,
                    rationale="无新证据",
                ),
            ),
        ]

    runner = AuditWorkflowRunner(router=router, output_root=tmp_path, cross_review_fn=_repeat_cross)
    res = runner.run(
        data_path=FIXTURES_DIR / "case_01_valid_claim.csv",
        raw_claim="发券组商户订单量高于未发券组。",
        initial_conflict_override={
            "full_claim_supported": False,
            "material_limitation": False,
            "unresolved_conflict": True,
        },
    )
    state = res["state"]
    assert state.round == 2
    assert state.review_closed is True
    assert state.duplicate_tool_executions == 0
    _assert_6_invariants(state)


def test_t06_missing_critical_field_insufficient_and_zero_judge_calls(tmp_path: Path) -> None:
    """T06: 关键字段缺失 -> insufficient，昂贵终裁调用 = 0。"""
    router = LLMRouter(mock_mode=True, log_dir=tmp_path)
    runner = AuditWorkflowRunner(router=router, output_root=tmp_path, final_judge_enabled=True)
    res = runner.run(
        data_path=FIXTURES_DIR / "case_05_missing_confounder.csv",
        raw_claim="发放消费券因果上使商户订单增加了 48 单。",
        context_notes="背景说明：同期存在大型促销活动，但当前导出表缺少大促参与字段。",
    )
    state = res["state"]
    assert state.status == VerdictStatus.INSUFFICIENT
    assert state.judge_calls == 0
    _assert_6_invariants(state)


def test_t07_final_judge_cites_nonexistent_evidence_id_rejected(tmp_path: Path) -> None:
    """T07: 终裁引用不存在的 evidence_id -> 机械拒绝输出。"""
    router = LLMRouter(mock_mode=True, log_dir=tmp_path)

    def _fake_ev_judge(ctx: BlindedJudgeInput, _: LLMRouter) -> Any:
        return {
            "action": "propose_mapping_correction",
            "claim_id": ctx.claim_id,
            "issue_id": ctx.unresolved_issue_id,
            "candidate_id": ctx.candidate_interpretations[0]["candidate_id"],
            "rule_id": "R1",
            "evidence_ids": ["ev_fabricated_999"],
            "reason_code": "fake_citation",
            "proposed_mapping": {"ev_fabricated_999": "supports"},
        }

    runner = AuditWorkflowRunner(
        router=router,
        output_root=tmp_path,
        final_judge_enabled=True,
        final_judge_fn=_fake_ev_judge,
    )
    res = runner.run(
        data_path=FIXTURES_DIR / "case_01_valid_claim.csv",
        raw_claim="发券组商户订单量高于未发券组。",
        initial_conflict_override={
            "full_claim_supported": False,
            "material_limitation": False,
            "unresolved_conflict": True,
            "conflict_type": "evidence_mapping",
            "conflict_is_material": True,
            "cheap_resolution_exhausted": True,
            "verification_path_exists": True,
            "review_closed": True,
        },
    )
    state = res["state"]
    assert state.judge_calls == 1
    assert state.stop_reason == "JUDGE_OUTPUT_REJECTED"
    assert state.status == VerdictStatus.INSUFFICIENT
    _assert_6_invariants(state)


def test_t08_final_judge_override_final_status_rejected(tmp_path: Path) -> None:
    """T08: 终裁试图覆盖硬规则或输出 final_status -> 拒绝越权。"""
    router = LLMRouter(mock_mode=True, log_dir=tmp_path)

    def _override_judge(ctx: BlindedJudgeInput, _: LLMRouter) -> Any:
        return {
            "action": "propose_mapping_correction",
            "claim_id": ctx.claim_id,
            "issue_id": ctx.unresolved_issue_id,
            "candidate_id": ctx.candidate_interpretations[0]["candidate_id"],
            "rule_id": "R1",
            "evidence_ids": [ctx.admissible_evidence[0].evidence_id],
            "reason_code": "force_override",
            "proposed_mapping": {},
            "final_status": "verified",
        }

    runner = AuditWorkflowRunner(
        router=router,
        output_root=tmp_path,
        final_judge_enabled=True,
        final_judge_fn=_override_judge,
    )
    res = runner.run(
        data_path=FIXTURES_DIR / "case_01_valid_claim.csv",
        raw_claim="发券组商户订单量高于未发券组。",
        initial_conflict_override={
            "full_claim_supported": False,
            "material_limitation": False,
            "unresolved_conflict": True,
            "conflict_type": "evidence_mapping",
            "conflict_is_material": True,
            "cheap_resolution_exhausted": True,
            "verification_path_exists": True,
            "review_closed": True,
        },
    )
    state = res["state"]
    assert state.judge_calls == 1
    assert state.judge_rule_overrides == 0
    assert state.stop_reason == "JUDGE_OUTPUT_REJECTED"
    assert state.status == VerdictStatus.INSUFFICIENT
    _assert_6_invariants(state)


def test_t09_budget_exhausted_terminates_cleanly_without_fabricating_evidence(tmp_path: Path) -> None:
    """T09: 重复请求或预算耗尽 -> 有限终止且不伪造证据。"""
    router = LLMRouter(mock_mode=True, max_budget_cny=0.0001, log_dir=tmp_path)
    runner = AuditWorkflowRunner(router=router, output_root=tmp_path)
    res = runner.run(
        data_path=FIXTURES_DIR / "case_01_valid_claim.csv",
        raw_claim="发券组商户订单量高于未发券组。",
        budget_cny=0.0001,
    )
    state = res["state"]
    assert state.stop_reason == "BUDGET_EXCEEDED"
    assert state.status == VerdictStatus.INSUFFICIENT
    assert len(state.evidence) == 0
    _assert_6_invariants(state)


def test_t10_both_models_fallback_to_qwen_sets_diversity_lost(tmp_path: Path) -> None:
    """T10: 双方降级同一模型（Qwen）-> 标记 model_diversity_lost=True。"""
    router = LLMRouter(
        mock_mode=True,
        log_dir=tmp_path,
        simulate_primary_failure_roles={"primary_reviewer", "challenger"},
    )
    runner = AuditWorkflowRunner(router=router, output_root=tmp_path)
    res = runner.run(
        data_path=FIXTURES_DIR / "case_01_valid_claim.csv",
        raw_claim="发券组商户订单量高于未发券组。",
    )
    state = res["state"]
    assert state.model_diversity_lost is True
    _assert_6_invariants(state)


def test_t11_judge_reports_unresolved_keeps_conflict_insufficient(tmp_path: Path) -> None:
    """T11: 无法客观核验终裁修正 -> 保留未决冲突，最终 insufficient。"""
    router = LLMRouter(mock_mode=True, log_dir=tmp_path)

    def _unresolved_judge(ctx: BlindedJudgeInput, _: LLMRouter) -> Any:
        return {
            "action": "report_unresolved",
            "claim_id": ctx.claim_id,
            "issue_id": ctx.unresolved_issue_id,
            "reason_code": "insufficient_objective_evidence",
            "unresolved_issue_ids": [ctx.unresolved_issue_id],
        }

    runner = AuditWorkflowRunner(
        router=router,
        output_root=tmp_path,
        final_judge_enabled=True,
        final_judge_fn=_unresolved_judge,
    )
    res = runner.run(
        data_path=FIXTURES_DIR / "case_01_valid_claim.csv",
        raw_claim="发券组商户订单量高于未发券组。",
        initial_conflict_override={
            "full_claim_supported": False,
            "material_limitation": False,
            "unresolved_conflict": True,
            "conflict_type": "evidence_mapping",
            "conflict_is_material": True,
            "cheap_resolution_exhausted": True,
            "verification_path_exists": True,
            "review_closed": True,
        },
    )
    state = res["state"]
    assert state.judge_calls == 1
    assert state.unresolved_conflict is True
    assert state.status == VerdictStatus.INSUFFICIENT
    _assert_6_invariants(state)


def test_t12_compound_causal_claim_not_misclassified_as_verified(tmp_path: Path) -> None:
    """T12: 复合命题中因果识别不足 -> 不将方向通过误判为整体 verified。"""
    router = LLMRouter(mock_mode=True, log_dir=tmp_path)
    runner = AuditWorkflowRunner(router=router, output_root=tmp_path)
    compound_claim = ClaimSpec(
        claim_id="claim_t12",
        raw_text="发放消费券使商户订单增加。",
        claim_type=ClaimType.CAUSAL,
        metric="orders_mean_diff",
        direction="increase",
        context_notes="同期存在大促但数据表缺少大促标记。",
        sub_claims=[
            SubClaim(sub_claim_id="t12_dir", claim_type=ClaimType.DIRECTION, metric="orders_mean_diff", direction="increase"),
            SubClaim(sub_claim_id="t12_causal", claim_type=ClaimType.CAUSAL, metric="orders_mean_diff", direction="increase"),
        ],
    )
    res = runner.run(
        data_path=FIXTURES_DIR / "case_05_missing_confounder.csv",
        raw_claim=compound_claim.raw_text,
        claim_spec=compound_claim,
    )
    state = res["state"]
    assert state.status != VerdictStatus.VERIFIED
    assert state.status == VerdictStatus.INSUFFICIENT
    _assert_6_invariants(state)


def test_audit_cli_end_to_end_case_03(capsys: object) -> None:
    rc = audit_cli_main(
        [
            "audit",
            "--data",
            str(FIXTURES_DIR / "case_03_promo_confounding.csv"),
            "--claim",
            "发放消费券使商户订单增加了 48 单。",
            "--trace",
            "--budget",
            "1.0",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "`CONTRADICTED`" in captured.out
    assert "Execution Trace" in captured.out


def test_stdio_deduplicates_defaults_and_preserves_rejected_required_check(tmp_path: Path) -> None:
    def review(claim, profile, _):
        params = {"dataset_id": profile.dataset_id, "target_col_id": "col_04", "group_col_id": "col_02"}
        requests = [
            ToolRequest(tool_id="group_compare", parameters=params),
            ToolRequest(tool_id="group_compare", parameters={**params, "metric": "mean", "stratify_col_id": None}),
            ToolRequest(tool_id="get_evidence"),
            ToolRequest(tool_id="inspect_data", parameters={"dataset_id": profile.dataset_id, "unexpected": 1}),
        ]
        issues = [IssueItem(issue_id=str(i), issue_type="metric", target_claim_id=claim.claim_id, requested_test=req)
                  for i, req in enumerate(requests)]
        out = BlindReviewOutput(claim_id=claim.claim_id, position=ReviewPosition.SUPPORT, issues=issues)
        return out, out, False

    runner = AuditWorkflowRunner(output_root=tmp_path, blind_review_fn=review)
    args = {"raw_claim": "发券组订单高于未发券组", "data_path": FIXTURES_DIR / "case_01_valid_claim.csv"}
    result = runner.run(**args, run_id="scoped_run")
    events = [entry.get("event") for entry in result["trace"]]
    assert events.count("tool_executed") == 1
    assert events.count("cache_hit") == events.count("ledger_read") == events.count("tool_rejected_or_failed") == 1
    assert result["state"].status == VerdictStatus.INSUFFICIENT
    assert not result["state"].required_checks_complete
    _assert_6_invariants(result["state"])
    with pytest.raises(FileExistsError):
        runner.run(**args, run_id="scoped_run")
    with pytest.raises(ValueError, match="conflict fixture"):
        runner.run(**args, initial_conflict_override={"decisive_refutation": True})


def test_tool_cap_retains_unexecuted_required_check(tmp_path: Path) -> None:
    runner = AuditWorkflowRunner(max_tool_calls=1, output_root=tmp_path)
    result = runner.run(raw_claim="发券组订单高于未发券组",
                        data_path=FIXTURES_DIR / "case_01_valid_claim.csv")
    state = result["state"]
    assert len(state.evidence) == 1
    assert state.pending_tests and not state.required_checks_complete
    assert state.status == VerdictStatus.INSUFFICIENT
    _assert_6_invariants(state)


def test_opposing_readings_escalate_once_and_accept_mechanically_grounded_mapping(tmp_path: Path) -> None:
    def opposing(claim, profile, _):
        req = ToolRequest(tool_id="group_compare", parameters={
            "dataset_id": profile.dataset_id, "target_col_id": "col_04", "group_col_id": "col_02"})
        issue = IssueItem(issue_id="comparison", issue_type="metric", target_claim_id=claim.claim_id,
                          requested_test=req)
        out = BlindReviewOutput(claim_id=claim.claim_id, position=ReviewPosition.SUPPORT, issues=[issue])
        return out, out.model_copy(update={"position": ReviewPosition.OPPOSE}), False

    def mapping(ctx, _):
        candidate = next(c for c in ctx.candidate_interpretations if c["relation"] == "supports")
        ids = candidate["cited_evidence_ids"]
        return {"action": "propose_mapping_correction", "claim_id": ctx.claim_id,
                "issue_id": ctx.unresolved_issue_id, "rule_id": candidate["rule_id"],
                "candidate_id": candidate["candidate_id"], "evidence_ids": ids,
                "reason_code": "positive_diff", "proposed_mapping": {eid: "supports" for eid in ids}}

    runner = AuditWorkflowRunner(output_root=tmp_path, blind_review_fn=opposing, final_judge_fn=mapping,
                                 final_judge_enabled=True)
    state = runner.run(raw_claim="发券组订单高于未发券组",
                       data_path=FIXTURES_DIR / "case_01_valid_claim.csv")["state"]
    assert state.review_closed and state.judge_closed and state.judge_calls == 1
    assert not state.unresolved_conflict and state.status == VerdictStatus.VERIFIED
    assert next(iter(state.issues.values())).evidence_ids == list(state.evidence)
    _assert_6_invariants(state)


def test_case03_marginal_direction_survives_zero_within_strata(tmp_path: Path) -> None:
    runner = AuditWorkflowRunner(output_root=tmp_path)
    state = runner.run(raw_claim="发券组订单总体均值高于未发券组。",
                       data_path=FIXTURES_DIR / "case_03_promo_confounding.csv")["state"]
    stratified = next(ev.result for ev in state.evidence.values() if ev.result.get("strata"))
    assert stratified["diff"] == 48 and all(row["diff"] == 0 for row in stratified["strata"])
    assert state.status == VerdictStatus.VERIFIED
