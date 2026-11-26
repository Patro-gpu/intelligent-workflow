"""Skill 4（确定性规则裁决与证据链核验报告）独立单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from core.schema import (
    BlindedJudgeInput,
    ClaimSpec,
    ClaimType,
    EvidenceRecord,
    IssueItem,
    IssueStatus,
    SubClaim,
    VerdictStatus,
    WorkflowState,
)
from skills.skill_4_evidence_auditor.cli import main as cli_main
from skills.skill_4_evidence_auditor.skill import (
    assess_robustness,
    build_audit_verdict,
    check_invariants,
    classify,
    evaluate_evidence_rules,
    render_markdown_report,
    validate_judge_output,
)


def _base_state(claim_id: str, evidence: list[EvidenceRecord], issues: list[IssueItem] | None = None) -> WorkflowState:
    return WorkflowState(
        run_id="run_test",
        claim_id=claim_id,
        dataset_hash="hash_test",
        evidence={ev.evidence_id: ev for ev in evidence},
        issues={iss.issue_id: iss for iss in (issues or [])},
    )


def test_winsorize_48_to_18_yields_limited_for_direction_claim() -> None:
    rob = assess_robustness(48.0, 18.0)
    assert rob["relative_change_ratio"] == 0.625
    assert rob["severity"] == "severe"
    assert rob["material_limitation"] is True

    claim = ClaimSpec(
        claim_id="c_dir",
        raw_text="发券组订单均值高于未发券组。",
        claim_type=ClaimType.DIRECTION,
        metric="orders_mean_diff",
        direction="increase",
    )
    ev_raw = EvidenceRecord(
        evidence_id="ev_raw",
        claim_id="c_dir",
        dataset_hash="h",
        tool_id="group_compare",
        sample_size=200,
        result={"diff": 48.0, "p_value": 0.01},
    )
    ev_sens = EvidenceRecord(
        evidence_id="ev_sens",
        claim_id="c_dir",
        dataset_hash="h",
        tool_id="sensitivity_check",
        sample_size=200,
        result={"baseline_diff": 48.0, "adjusted_diff": 18.0, "method": "winsorize_5pct"},
        limitations=["5% 缩尾后均值差从 48 衰减至 18（相对变化率 62.5% > 50%）。"],
    )
    evaluated = evaluate_evidence_rules(claim, _base_state("c_dir", [ev_raw, ev_sens]))
    assert classify(evaluated) == VerdictStatus.LIMITED


def test_universal_claim_subgroup_reversal_yields_contradicted() -> None:
    claim = ClaimSpec(
        claim_id="c_univ",
        raw_text="所有子组中发券组订单量均高于未发券组。",
        claim_type=ClaimType.UNIVERSAL,
        metric="orders_mean_diff",
        direction="increase",
    )
    ev_strat = EvidenceRecord(
        evidence_id="ev_strat",
        claim_id="c_univ",
        dataset_hash="h",
        tool_id="group_compare",
        sample_size=200,
        result={
            "diff": 15.0,
            "strata": [
                {"stratum": "promo_1", "diff": 25.0},
                {"stratum": "promo_0", "diff": -8.0},
            ],
        },
    )
    evaluated = evaluate_evidence_rules(claim, _base_state("c_univ", [ev_strat]))
    assert classify(evaluated) == VerdictStatus.CONTRADICTED


def test_denominator_error_contradicts_magnitude_claim() -> None:
    claim = ClaimSpec(
        claim_id="c_denom",
        raw_text="本期营收增长了 50%。",
        claim_type=ClaimType.MAGNITUDE,
        metric="revenue_growth_rate",
        direction="increase",
        target_value=50.0,
        unit="percent",
    )
    ev_agg = EvidenceRecord(
        evidence_id="ev_agg",
        claim_id="c_denom",
        dataset_hash="h",
        tool_id="aggregate",
        sample_size=20,
        result={"base_sum": 100.0, "current_sum": 125.0, "growth_rate_pct": 25.0},
    )
    evaluated = evaluate_evidence_rules(claim, _base_state("c_denom", [ev_agg]))
    assert classify(evaluated) == VerdictStatus.CONTRADICTED


def test_compound_causal_claim_gap_never_misclassified_as_verified() -> None:
    claim = ClaimSpec(
        claim_id="c_causal_gap",
        raw_text="发放消费券因果上使商户订单增加。",
        claim_type=ClaimType.CAUSAL,
        metric="orders_mean_diff",
        direction="increase",
        sub_claims=[
            SubClaim(sub_claim_id="s1", claim_type=ClaimType.DIRECTION, metric="orders_mean_diff", direction="increase"),
            SubClaim(sub_claim_id="s2", claim_type=ClaimType.CAUSAL, metric="orders_mean_diff", direction="increase"),
        ],
    )
    ev_raw = EvidenceRecord(
        evidence_id="ev_raw",
        claim_id="c_causal_gap",
        dataset_hash="h",
        tool_id="group_compare",
        sample_size=200,
        result={"diff": 48.0, "p_value": 0.001},
    )
    gap_issue = IssueItem(
        issue_id="iss_gap",
        issue_type="missing_field",
        target_claim_id="c_causal_gap",
        status=IssueStatus.UNRESOLVABLE,
        rationale="缺少大促混淆字段 is_promo",
    )
    evaluated = evaluate_evidence_rules(claim, _base_state("c_causal_gap", [ev_raw], issues=[gap_issue]))
    assert evaluated.critical_gap is True
    assert classify(evaluated) == VerdictStatus.INSUFFICIENT


def test_validate_judge_output_blocks_overreach_and_fake_evidence() -> None:
    claim = ClaimSpec(
        claim_id="c1",
        raw_text="orders increased",
        claim_type=ClaimType.DIRECTION,
        metric="orders",
        direction="increase",
    )
    ev_ok = EvidenceRecord(
        evidence_id="ev_001",
        claim_id="c1",
        dataset_hash="h",
        tool_id="group_compare",
        result={"diff": 12.0},
    )
    ctx = BlindedJudgeInput(
        claim_id="c1",
        rule_version="1.0",
        unresolved_issue_id="iss_1",
        issue_type="evidence_mapping",
        claim_spec=claim,
        candidate_interpretations=[{"candidate_id": "cand_a", "summary": "A"}],
        admissible_evidence=[ev_ok],
    )

    # 1. 越权塞入 final_status -> 机械拦截
    ok_override, _, reasons_override = validate_judge_output(
        {
            "action": "propose_mapping_correction",
            "claim_id": "c1",
            "issue_id": "iss_1",
            "candidate_id": "cand_a",
            "rule_id": "r1",
            "evidence_ids": ["ev_001"],
            "reason_code": "fix",
            "proposed_mapping": {"ev_001": "supports"},
            "final_status": "verified",
        },
        ctx,
    )
    assert ok_override is False
    assert reasons_override

    # 2. 引用不存在的 evidence_id -> 机械拦截
    ok_fake, _, reasons_fake = validate_judge_output(
        {
            "action": "propose_mapping_correction",
            "claim_id": "c1",
            "issue_id": "iss_1",
            "candidate_id": "cand_a",
            "rule_id": "r1",
            "evidence_ids": ["ev_999_does_not_exist"],
            "reason_code": "fix",
            "proposed_mapping": {"ev_999_does_not_exist": "supports"},
        },
        ctx,
    )
    assert ok_fake is False
    assert any("non-existent" in r for r in reasons_fake)

    # 3. 合法映射修正 -> 通过机械核验
    ok_valid, parsed, reasons_valid = validate_judge_output(
        {
            "action": "propose_mapping_correction",
            "claim_id": "c1",
            "issue_id": "iss_1",
            "candidate_id": "cand_a",
            "rule_id": "r1",
            "evidence_ids": ["ev_001"],
            "reason_code": "scope_aligned",
            "proposed_mapping": {"ev_001": "supports"},
        },
        ctx,
    )
    assert ok_valid is True
    assert parsed is not None
    assert reasons_valid == []


def test_markdown_report_and_cli_report(tmp_path: Path) -> None:
    claim = ClaimSpec(
        claim_id="c_ok",
        raw_text="发券组订单均值高于未发券组。",
        claim_type=ClaimType.DIRECTION,
        metric="orders_mean_diff",
        direction="increase",
    )
    ev = EvidenceRecord(
        evidence_id="ev_101",
        claim_id="c_ok",
        dataset_hash="h",
        tool_id="group_compare",
        sample_size=120,
        result={"diff": 30.0, "p_value": 0.001},
    )
    state = evaluate_evidence_rules(claim, _base_state("c_ok", [ev]))
    assert classify(state) == VerdictStatus.VERIFIED
    inv_ok, _ = check_invariants(state)
    assert inv_ok is True

    verdict = build_audit_verdict(claim, state)
    md = render_markdown_report(claim, verdict, state)
    assert "[ev_101]" in md
    assert "`VERIFIED`" in md

    # 测试独立 /report CLI
    bundle_path = tmp_path / "bundle.json"
    out_md = tmp_path / "report.md"
    bundle_path.write_text(
        json.dumps(
            {
                "claim_spec": claim.model_dump(mode="json"),
                "workflow_state": _base_state("c_ok", [ev]).model_dump(mode="json"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rc = cli_main(["report", "--input", str(bundle_path), "--output", str(out_md)])
    assert rc == 0
    assert "`VERIFIED`" in out_md.read_text(encoding="utf-8")
