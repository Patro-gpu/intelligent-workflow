"""《数证 Audit》6 阶段有界状态机（`workflows.state_machine`）。

严格实现 `docs/BLUEPRINT_v2.md` 规定的 6 个有界阶段与硬守卫：
1. `INIT`
2. `BLIND_REVIEW`（第 1 轮并行盲审）
3. `PLAN_AND_RUN_MCP`（去重执行受控白名单统计工具）
4. `RULE_CHECK`（确定性状态位计算与优先级路由）
5. `CROSS_REVIEW`（第 2 轮 5 枚举增量验证）/ `FINAL_JUDGE`（盲化复核，默认关闭且 <= 1 次）
6. `FINALIZE`（纯确定性 `classify(state)` 与证据链报告落盘）
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from core.llm_router import BudgetExceededError, LLMRouter
from core.schema import (
    AuditVerdict,
    BlindReviewOutput,
    BlindedJudgeInput,
    ClaimSpec,
    CrossReviewDelta,
    DatasetProfile,
    EvidenceRecord,
    JudgeMappingCorrection,
    JudgeUnresolvedReport,
    ReviewAction,
    VerificationTask,
    VerdictStatus,
    WorkflowState,
)
from core.security import resolve_safe_path
from skills.skill_2_stat_mcp.engine import StatAuditEngine
from skills.skill_3_claim_planner.skill import (
    ALLOWED_MCP_TOOLS,
    deduplicate_and_plan_tasks,
    evaluate_cross_review_deltas,
    freeze_claim,
    load_prompt_template,
    run_blind_review,
)
from skills.skill_4_evidence_auditor.skill import (
    build_audit_verdict,
    check_invariants,
    classify,
    evaluate_evidence_rules,
    render_markdown_report,
    validate_judge_output,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def should_escalate(state: WorkflowState, *, final_judge_enabled: bool = False) -> bool:
    """严控终裁门控：全部 12 项条件同时满足才允许触发盲化终裁（最多 1 次）。"""
    return (
        final_judge_enabled
        and not state.judge_closed
        and state.judge_calls < 1
        and state.required_checks_complete
        and not state.critical_gap
        and state.unresolved_conflict
        and not state.decisive_refutation
        and state.conflict_type == "evidence_mapping"
        and state.conflict_is_material
        and state.cheap_resolution_exhausted
        and state.verification_path_exists
        and not state.pending_tests
        and state.remaining_budget >= state.reserved_judge_cost
    )


def build_blinded_judge_input(
    *,
    claim_spec: ClaimSpec,
    state: WorkflowState,
    unresolved_issue_id: str,
    candidate_interpretations: List[Dict[str, Any]],
    missing_checks: Optional[List[str]] = None,
) -> BlindedJudgeInput:
    """构造完全盲化的终裁输入：剥离模型名、置信度与自由文本，并对候选做可复现随机排序。"""
    seed_int = int(hashlib.sha256(f"{claim_spec.claim_id}:{unresolved_issue_id}".encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed_int)
    sanitized_candidates: List[Dict[str, Any]] = []
    for idx, cand in enumerate(candidate_interpretations):
        sanitized_candidates.append(
            {
                "candidate_id": str(cand.get("candidate_id") or f"cand_{idx + 1}"),
                "interpretation": str(cand.get("interpretation") or cand.get("summary") or ""),
                "cited_evidence_ids": list(cand.get("cited_evidence_ids", [])),
            }
        )
    rng.shuffle(sanitized_candidates)

    admissible_ev = [
        ev for ev in state.evidence.values() if ev.execution_status == "ok" and ev.applicable
    ]
    return BlindedJudgeInput(
        claim_id=claim_spec.claim_id,
        rule_version=state.rule_version,
        unresolved_issue_id=unresolved_issue_id,
        issue_type=state.conflict_type or "evidence_mapping",
        claim_spec=claim_spec,
        candidate_interpretations=sanitized_candidates,
        admissible_evidence=admissible_ev,
        missing_checks=list(missing_checks or []),
    )


class AuditWorkflowRunner:
    """6 阶段有界审计状态机运行器。"""

    def __init__(
        self,
        *,
        router: Optional[LLMRouter] = None,
        stat_engine: Optional[StatAuditEngine] = None,
        output_root: str | Path = REPO_ROOT / "workspace/outputs",
        final_judge_enabled: bool = False,
        max_tool_calls: int = 12,
        blind_review_fn: Optional[
            Callable[[ClaimSpec, DatasetProfile, LLMRouter], Tuple[BlindReviewOutput, BlindReviewOutput, bool]]
        ] = None,
        cross_review_fn: Optional[
            Callable[[ClaimSpec, WorkflowState], List[Tuple[str, CrossReviewDelta]]]
        ] = None,
        final_judge_fn: Optional[Callable[[BlindedJudgeInput, LLMRouter], Any]] = None,
    ) -> None:
        self.router = router or LLMRouter(mock_mode=True)
        self.output_root = Path(output_root).expanduser().resolve()
        self.stat_engine = stat_engine or StatAuditEngine(output_root=self.output_root)
        self.final_judge_enabled = final_judge_enabled
        self.max_tool_calls = max_tool_calls
        self._blind_review_fn = blind_review_fn
        self._cross_review_fn = cross_review_fn
        self._final_judge_fn = final_judge_fn

    def _execute_single_task(
        self,
        task: VerificationTask,
        *,
        run_id: str,
        claim_id: str,
    ) -> Optional[EvidenceRecord]:
        if task.tool_id not in ALLOWED_MCP_TOOLS:
            return None

        params = dict(task.parameters)
        try:
            if task.tool_id == "inspect_data":
                return self.stat_engine.inspect_data(
                    params["dataset_id"], run_id=run_id, claim_id=claim_id
                )
            if task.tool_id == "aggregate":
                return self.stat_engine.aggregate(
                    params["dataset_id"],
                    params["target_col_id"],
                    params.get("metric", "sum"),
                    filters=params.get("filters"),
                    run_id=run_id,
                    claim_id=claim_id,
                )
            if task.tool_id == "group_compare":
                return self.stat_engine.group_compare(
                    params["dataset_id"],
                    params["target_col_id"],
                    params["group_col_id"],
                    metric=params.get("metric", "mean"),
                    stratify_col_id=params.get("stratify_col_id"),
                    run_id=run_id,
                    claim_id=claim_id,
                )
            if task.tool_id == "sensitivity_check":
                return self.stat_engine.sensitivity_check(
                    params["dataset_id"],
                    params["target_col_id"],
                    params["group_col_id"],
                    method=params.get("method", "winsorize_5pct"),
                    run_id=run_id,
                    claim_id=claim_id,
                )
        except Exception:  # noqa: BLE001
            return None
        return None

    def _run_pending_mcp(
        self,
        state: WorkflowState,
        trace: List[Dict[str, Any]],
    ) -> WorkflowState:
        """阶段 3：去重执行待运行白名单统计任务，绝不重复执行同 request_key 或在终态后调用。"""
        if state.status is not None:
            return state

        executed_count = 0
        while state.pending_tests:
            task = state.pending_tests.pop(0)
            if task.request_key in state.executed_tests:
                # 命中已执行缓存直接复用证据，零重复工具执行
                trace.append(
                    {
                        "stage": "PLAN_AND_RUN_MCP",
                        "event": "cache_hit",
                        "request_key": task.request_key,
                        "evidence_id": state.executed_tests[task.request_key],
                    }
                )
                continue

            if state.remaining_tool_calls <= 0:
                state.stop_reason = "TOOL_CALL_LIMIT_REACHED"
                trace.append({"stage": "PLAN_AND_RUN_MCP", "event": "tool_limit_reached"})
                break

            state.remaining_tool_calls -= 1
            rec = self._execute_single_task(task, run_id=state.run_id, claim_id=state.claim_id)
            if rec is not None and rec.execution_status == "ok":
                state.evidence[rec.evidence_id] = rec
                state.executed_tests[task.request_key] = rec.evidence_id
                executed_count += 1
                trace.append(
                    {
                        "stage": "PLAN_AND_RUN_MCP",
                        "event": "tool_executed",
                        "tool_id": task.tool_id,
                        "evidence_id": rec.evidence_id,
                    }
                )
            else:
                trace.append(
                    {
                        "stage": "PLAN_AND_RUN_MCP",
                        "event": "tool_rejected_or_failed",
                        "tool_id": task.tool_id,
                    }
                )

        state.new_information = executed_count > 0
        return state

    def run(
        self,
        *,
        data_path: str | Path,
        raw_claim: str,
        claim_spec: Optional[ClaimSpec] = None,
        context_notes: str = "",
        run_id: Optional[str] = None,
        budget_cny: Optional[float] = None,
        persist_outputs: bool = True,
        initial_conflict_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """运行完整 6 阶段有界状态机，返回 `state`, `verdict`, `report_markdown`, `trace`, `output_dir`。"""
        assigned_run_id = run_id or f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        trace: List[Dict[str, Any]] = []

        if budget_cny is not None:
            self.router.max_budget_cny = float(budget_cny)

        # ------------------------------------------------------------------
        # Stage 1: INIT
        # ------------------------------------------------------------------
        if not raw_claim.strip() and claim_spec is None:
            empty_claim = ClaimSpec(
                claim_id="claim_empty",
                raw_text="",
                claim_type="descriptive",  # type: ignore[arg-type]
                metric="unknown",
            )
            state = WorkflowState(
                run_id=assigned_run_id,
                claim_id=empty_claim.claim_id,
                dataset_hash="",
                critical_gap=True,
                status=VerdictStatus.INSUFFICIENT,
                stop_reason="INIT_INVALID_INPUT",
                remaining_budget=self.router.remaining_budget_cny,
                remaining_tool_calls=self.max_tool_calls,
            )
            verdict = build_audit_verdict(empty_claim, state)
            report = render_markdown_report(empty_claim, verdict, state)
            return {"state": state, "verdict": verdict, "report_markdown": report, "trace": trace, "output_dir": None}

        prof_dict = self.stat_engine.register_dataset(data_path)
        profile = DatasetProfile.model_validate(prof_dict)
        frozen_claim = claim_spec or freeze_claim(
            raw_claim,
            claim_id=f"claim_{assigned_run_id[-6:]}",
            context_notes=context_notes,
            router=self.router,
        )
        state = WorkflowState(
            run_id=assigned_run_id,
            round=1,
            claim_id=frozen_claim.claim_id,
            dataset_hash=profile.dataset_hash,
            rule_version="1.0",
            remaining_budget=self.router.remaining_budget_cny,
            reserved_judge_cost=0.10,
            remaining_tool_calls=self.max_tool_calls,
        )
        trace.append({"stage": "INIT", "claim_id": frozen_claim.claim_id, "dataset_id": profile.dataset_id})

        # ------------------------------------------------------------------
        # Stage 2: BLIND_REVIEW (Round 1)
        # ------------------------------------------------------------------
        try:
            if self._blind_review_fn is not None:
                p_out, c_out, diversity_lost = self._blind_review_fn(frozen_claim, profile, self.router)
            else:
                p_out, c_out, diversity_lost = run_blind_review(frozen_claim, profile, router=self.router)
            state.model_diversity_lost = diversity_lost
            state.remaining_budget = self.router.remaining_budget_cny

            issues_map, tasks, _ = deduplicate_and_plan_tasks(
                [("primary_reviewer", p_out), ("challenger", c_out)],
                dataset_hash=profile.dataset_hash,
            )
            state.issues.update(issues_map)
            state.pending_tests.extend(tasks)
            trace.append(
                {
                    "stage": "BLIND_REVIEW",
                    "primary_position": p_out.position.value,
                    "challenger_position": c_out.position.value,
                    "model_diversity_lost": diversity_lost,
                    "planned_tasks": len(tasks),
                }
            )
        except BudgetExceededError:
            state.remaining_budget = self.router.remaining_budget_cny
            state.stop_reason = "BUDGET_EXCEEDED"
            trace.append({"stage": "BLIND_REVIEW", "event": "budget_exceeded"})
            return self._finalize(frozen_claim, state, trace, persist_outputs=persist_outputs)

        # ------------------------------------------------------------------
        # Stage 3 -> 4 -> (5) Bounded Loop
        # ------------------------------------------------------------------
        max_iterations = 8
        for _ in range(max_iterations):
            if state.pending_tests and state.remaining_tool_calls > 0:
                state = self._run_pending_mcp(state, trace)

            # Stage 4: RULE_CHECK
            state = evaluate_evidence_rules(frozen_claim, state)
            state.remaining_budget = self.router.remaining_budget_cny

            if initial_conflict_override:
                for key, val in initial_conflict_override.items():
                    if hasattr(state, key):
                        setattr(state, key, val)
                initial_conflict_override = None

            trace.append(
                {
                    "stage": "RULE_CHECK",
                    "round": state.round,
                    "decisive_refutation": state.decisive_refutation,
                    "critical_gap": state.critical_gap,
                    "full_claim_supported": state.full_claim_supported,
                    "core_supported": state.core_supported,
                    "material_limitation": state.material_limitation,
                    "unresolved_conflict": state.unresolved_conflict,
                }
            )

            # 路由优先级 1: 有效决定性反证 -> 立即终止 contradicted
            if state.decisive_refutation:
                state.stop_reason = "DECISIVE_REFUTATION"
                break

            # 路由优先级 2: 存在合法待跑检验且未超调用上限 -> 回 PLAN_AND_RUN_MCP（不加轮次）
            if state.pending_tests and state.remaining_tool_calls > 0:
                continue

            # 路由优先级 3: 提前终止 verified / limited
            if state.required_checks_complete and state.full_claim_supported and not state.material_limitation:
                state.stop_reason = "EARLY_STOP_VERIFIED"
                break
            if (
                state.required_checks_complete
                and state.core_supported
                and state.material_limitation
                and not state.unresolved_conflict
            ):
                state.stop_reason = "EARLY_STOP_LIMITED"
                break

            # 路由优先级 4: 关键数据/因果识别缺口 -> 立即终止 insufficient（禁止唤醒昂贵终裁）
            if state.critical_gap:
                state.stop_reason = "CRITICAL_GAP"
                break

            # 路由优先级 5: 第 1 轮存在未决争议且未关闭辩论 -> 进入第 2 轮 CROSS_REVIEW
            if state.round == 1 and not state.review_closed and state.unresolved_conflict and state.remaining_budget > 0:
                state.round = 2
                if self._cross_review_fn is not None:
                    deltas = self._cross_review_fn(frozen_claim, state)
                else:
                    deltas = [
                        (
                            "primary_reviewer",
                            CrossReviewDelta(
                                claim_id=frozen_claim.claim_id,
                                action=ReviewAction.NO_NEW_EVIDENCE,
                                rationale="No additional whitelisted tests proposed.",
                            ),
                        )
                    ]

                new_tasks, made_progress, stop_reason = evaluate_cross_review_deltas(
                    deltas,
                    dataset_hash=state.dataset_hash,
                    executed_request_keys=set(state.executed_tests.keys()),
                    existing_evidence_ids=set(state.evidence.keys()),
                )
                state.review_closed = True
                trace.append(
                    {
                        "stage": "CROSS_REVIEW",
                        "new_tasks": len(new_tasks),
                        "made_progress": made_progress,
                        "stop_reason": stop_reason,
                    }
                )
                if not made_progress:
                    state.stop_reason = stop_reason or "NO_PROGRESS"
                    state.cheap_resolution_exhausted = True
                    # 仍需经 RULE_CHECK 判断是否满足终裁门控或直接收敛
                    if not should_escalate(state, final_judge_enabled=self.final_judge_enabled):
                        break
                else:
                    state.pending_tests.extend(new_tasks)
                    continue

            # 路由优先级 6: 满足严格门控 -> 盲化进入 FINAL_JUDGE（最多 1 次）
            if should_escalate(state, final_judge_enabled=self.final_judge_enabled):
                state.judge_calls += 1
                state.judge_closed = True
                state.remaining_budget = max(0.0, round(state.remaining_budget - state.reserved_judge_cost, 8))

                unresolved_issue_id = next(iter(state.issues.keys()), "iss_mapping")
                blinded_input = build_blinded_judge_input(
                    claim_spec=frozen_claim,
                    state=state,
                    unresolved_issue_id=unresolved_issue_id,
                    candidate_interpretations=[
                        {
                            "candidate_id": "cand_support",
                            "interpretation": "Evidence supports core claim scope.",
                            "cited_evidence_ids": list(state.evidence.keys()),
                        },
                        {
                            "candidate_id": "cand_limit",
                            "interpretation": "Evidence indicates material limitation.",
                            "cited_evidence_ids": list(state.evidence.keys()),
                        },
                    ],
                )

                if self._final_judge_fn is not None:
                    raw_judge_out = self._final_judge_fn(blinded_input, self.router)
                else:
                    judge_prompt = load_prompt_template("final_judge")
                    resp = self.router.complete(
                        role="final_reviewer",
                        messages=[
                            {"role": "system", "content": judge_prompt},
                            {"role": "user", "content": blinded_input.model_dump_json()},
                        ],
                        expect_json=True,
                        mock_payload={
                            "action": "report_unresolved",
                            "claim_id": frozen_claim.claim_id,
                            "issue_id": unresolved_issue_id,
                            "reason_code": "mock_unresolved",
                            "unresolved_issue_ids": [unresolved_issue_id],
                        },
                    )
                    raw_judge_out = resp.parsed_json

                is_valid, parsed_out, reject_reasons = validate_judge_output(raw_judge_out, blinded_input)
                trace.append(
                    {
                        "stage": "FINAL_JUDGE",
                        "valid": is_valid,
                        "reject_reasons": reject_reasons,
                    }
                )
                if is_valid and isinstance(parsed_out, JudgeMappingCorrection):
                    state.unresolved_conflict = False
                    state.stop_reason = f"JUDGE_MAPPING_VERIFIED:{parsed_out.candidate_id}"
                    continue
                if is_valid and isinstance(parsed_out, JudgeUnresolvedReport):
                    state.unresolved_conflict = True
                    state.stop_reason = f"JUDGE_REPORTED_UNRESOLVED:{parsed_out.reason_code}"
                    break

                # 机械校验拒绝越权或伪造证据 ID -> 绝不采纳，绝不覆盖硬规则
                state.unresolved_conflict = True
                state.stop_reason = "JUDGE_OUTPUT_REJECTED"
                break

            break

        return self._finalize(frozen_claim, state, trace, persist_outputs=persist_outputs)

    def _finalize(
        self,
        claim_spec: ClaimSpec,
        state: WorkflowState,
        trace: List[Dict[str, Any]],
        *,
        persist_outputs: bool,
    ) -> Dict[str, Any]:
        """Stage 6: FINALIZE — 纯确定性 `classify(state)` 与审计报告落盘。"""
        state.status = classify(state)
        verdict = build_audit_verdict(claim_spec, state)
        inv_ok, violations = check_invariants(state)
        report_md = render_markdown_report(claim_spec, verdict, state)
        trace.append(
            {
                "stage": "FINALIZE",
                "status": state.status.value,
                "stop_reason": state.stop_reason,
                "invariants_passed": inv_ok,
                "violations": violations,
            }
        )

        out_dir: Optional[Path] = None
        if persist_outputs:
            safe_run = "".join(ch for ch in state.run_id if ch.isalnum() or ch in ("-", "_")) or "run_default"
            out_dir = self.output_root / safe_run
            out_dir.mkdir(parents=True, exist_ok=True)

            report_file = resolve_safe_path(out_dir / "report.md", [self.output_root])
            verdict_file = resolve_safe_path(out_dir / "verdict.json", [self.output_root])
            trace_file = resolve_safe_path(out_dir / "trace.json", [self.output_root])

            report_file.write_text(report_md, encoding="utf-8")
            verdict_file.write_text(verdict.model_dump_json(indent=2), encoding="utf-8")
            trace_file.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")

        return {
            "state": state,
            "verdict": verdict,
            "report_markdown": report_md,
            "trace": trace,
            "output_dir": str(out_dir) if out_dir else None,
        }
