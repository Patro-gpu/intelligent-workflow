"""Skill 3（命题冻结与双轮增量验证规划）独立单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from core.llm_router import LLMRouter
from core.schema import (
    BlindReviewOutput,
    ClaimType,
    CrossReviewDelta,
    IssueItem,
    ReviewAction,
    ReviewPosition,
    ToolRequest,
    compute_request_key,
)
from core.security import DatasetRegistry
from skills.skill_3_claim_planner.cli import main as cli_main
from skills.skill_3_claim_planner.skill import (
    deduplicate_and_plan_tasks,
    evaluate_cross_review_deltas,
    freeze_claim,
    run_blind_review,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
FIXTURES_DIR = REPO_ROOT / "data/fixtures"


def test_freeze_claim_covers_5_types_and_subclaim_cap() -> None:
    c_causal = freeze_claim("发放消费券使商户订单增加了 48 单。", claim_id="c3")
    assert c_causal.claim_type == ClaimType.CAUSAL
    assert c_causal.target_value == 48.0
    assert len(c_causal.sub_claims) == 2

    c_univ = freeze_claim("所有商户发券后订单量均高于未发券组。", claim_id="c_u")
    assert c_univ.claim_type == ClaimType.UNIVERSAL
    assert len(c_univ.sub_claims) <= 2

    c_mag = freeze_claim("本期商户总营收较基期增长了 50%。", claim_id="c2")
    assert c_mag.claim_type == ClaimType.MAGNITUDE
    assert c_mag.target_value == 50.0
    assert c_mag.unit == "percent"

    c_dir = freeze_claim("发券组订单量高于未发券组。", claim_id="c_d")
    assert c_dir.claim_type == ClaimType.DIRECTION

    c_desc = freeze_claim("发券组订单均值表现平稳。", claim_id="c_desc")
    assert c_desc.claim_type == ClaimType.DESCRIPTIVE


def test_blind_review_plans_stratification_for_case_03_and_gap_for_case_05(tmp_path: Path) -> None:
    registry = DatasetRegistry(allowed_roots=[FIXTURES_DIR])
    router = LLMRouter(mock_mode=True, log_dir=tmp_path)

    # Case 03: 有 is_promo 字段 -> 规划 group_compare(stratify_col_id=...) 与 sensitivity_check
    prof_03 = registry.register_csv(FIXTURES_DIR / "case_03_promo_confounding.csv", dataset_id="case_03")
    claim_03 = freeze_claim("发放消费券使商户订单增加了 48 单。", claim_id="claim_03")
    p_out, c_out, div_lost = run_blind_review(claim_03, prof_03, router=router)
    assert div_lost is False
    _, tasks, _ = deduplicate_and_plan_tasks(
        [("primary_reviewer", p_out), ("challenger", c_out)],
        dataset_hash=prof_03.dataset_hash,
    )
    tool_ids = [t.tool_id for t in tasks]
    assert "group_compare" in tool_ids
    assert "sensitivity_check" in tool_ids
    assert any("stratify_col_id" in t.parameters for t in tasks if t.tool_id == "group_compare")

    # Case 05: 缺少 is_promo 字段但背景提及大促 -> 反方标记 missing_field 缺口
    prof_05 = registry.register_csv(FIXTURES_DIR / "case_05_missing_confounder.csv", dataset_id="case_05")
    claim_05 = freeze_claim(
        "发放消费券因果上使商户订单增加了 48 单。",
        claim_id="claim_05",
        context_notes="同期存在大型促销活动，但导出表缺失大促标记。",
    )
    _, c_out_05, _ = run_blind_review(claim_05, prof_05, router=router)
    assert any(iss.issue_type == "missing_field" for iss in c_out_05.issues)


def test_deduplicate_tasks_blocks_duplicates_and_non_whitelist_tools() -> None:
    req = ToolRequest(tool_id="group_compare", parameters={"dataset_id": "d1", "target_col_id": "col_04"})
    evil_req = ToolRequest(tool_id="exec_python", parameters={"code": "import os"})
    r1 = BlindReviewOutput(
        claim_id="c1",
        position=ReviewPosition.UNCERTAIN,
        issues=[IssueItem(issue_id="i1", issue_type="metric", target_claim_id="c1", requested_test=req)],
    )
    r2 = BlindReviewOutput(
        claim_id="c1",
        position=ReviewPosition.OPPOSE,
        issues=[
            IssueItem(issue_id="i2", issue_type="metric", target_claim_id="c1", requested_test=req),
            IssueItem(issue_id="i3", issue_type="evil", target_claim_id="c1", requested_test=evil_req),
        ],
    )
    issues, tasks, skipped = deduplicate_and_plan_tasks([("p", r1), ("c", r2)], dataset_hash="hash_1")
    assert len(tasks) == 1
    assert skipped == 1
    assert len(issues) == 2  # 越权请求被拒但对应争议记录不被悄然删除


def test_cross_review_anti_repetition_and_no_ungrounded_compromise() -> None:
    ds_hash = "hash_abc"
    params = {"dataset_id": "d1", "target_col_id": "col_04", "group_col_id": "col_02", "metric": "mean"}
    already_run = compute_request_key(ds_hash, "group_compare", "1.0", params)

    # 1. 复读已执行检验 + 无证据撤回异议 -> NO_PROGRESS
    deltas_repeat = [
        (
            "primary_reviewer",
            CrossReviewDelta(
                claim_id="c1",
                action=ReviewAction.ADD_NEW_TEST,
                requested_test=ToolRequest(tool_id="group_compare", parameters=params),
                rationale="再跑一次相同的测试",
            ),
        ),
        (
            "challenger",
            CrossReviewDelta(
                claim_id="c1",
                action=ReviewAction.WITHDRAW_OBJECTION,
                target_evidence_id=None,
                rationale="好吧我同意你",
            ),
        ),
    ]
    tasks, made_progress, stop_reason = evaluate_cross_review_deltas(
        deltas_repeat,
        dataset_hash=ds_hash,
        executed_request_keys={already_run},
        existing_evidence_ids={"ev_001"},
    )
    assert tasks == []
    assert made_progress is False
    assert stop_reason == "NO_PROGRESS"

    # 2. 提交全新合法白名单检验 -> 入队且有进展
    new_params = {"dataset_id": "d1", "target_col_id": "col_04", "group_col_id": "col_02", "method": "winsorize_5pct"}
    deltas_new = [
        (
            "challenger",
            CrossReviewDelta(
                claim_id="c1",
                action=ReviewAction.ADD_NEW_TEST,
                requested_test=ToolRequest(tool_id="sensitivity_check", parameters=new_params),
                rationale="补充 5% 缩尾敏感性检验",
            ),
        )
    ]
    tasks2, made_progress2, stop_reason2 = evaluate_cross_review_deltas(
        deltas_new,
        dataset_hash=ds_hash,
        executed_request_keys={already_run},
        existing_evidence_ids={"ev_001"},
    )
    assert len(tasks2) == 1
    assert made_progress2 is True
    assert stop_reason2 == ""


def test_skill_3_cli_challenge_runs_offline(capsys: object) -> None:
    rc = cli_main(
        [
            "challenge",
            "--data",
            str(FIXTURES_DIR / "case_03_promo_confounding.csv"),
            "--claim",
            "发放消费券使商户订单增加了 48 单。",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["claim_spec"]["claim_type"] == "causal"
    assert payload["planned_task_count"] >= 2
