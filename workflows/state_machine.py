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

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import math
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple
import uuid

from core.llm_router import BudgetExceededError, LLMRouter, ModelCallError
from core.schema import (
    canonical_json,
    compute_request_key,
    BlindReviewOutput,
    BlindedJudgeInput,
    ClaimSpec,
    CrossReviewDelta,
    DatasetProfile,
    EvidenceRecord,
    IssueStatus,
    JudgeMappingCorrection,
    JudgeUnresolvedReport,
    ReviewAction,
    ReviewPosition,
    VerificationTask,
    WorkflowState,
)
from core.mcp_client import StatMCPClient
from core.security import SecurityError, resolve_safe_path, sandbox_root, validate_run_id
from skills.skill_1_data_profiler.skill import build_default_registry

if TYPE_CHECKING:
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


# Compatibility with the offline T05/T07/T08/T11 dispute fixtures.
_MOCK_CONFLICT_FIELDS = {
    "full_claim_supported": False, "material_limitation": False, "unresolved_conflict": True,
    "conflict_type": "evidence_mapping", "conflict_is_material": True,
    "cheap_resolution_exhausted": True, "verification_path_exists": True, "review_closed": True,
}


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
    admissible_ev = [
        ev for eid, ev in state.evidence.items() if eid == ev.evidence_id and ev.execution_status == "ok"
        and ev.applicable and ev.claim_id == state.claim_id and ev.dataset_hash == state.dataset_hash
    ]
    allowed_ids = {ev.evidence_id for ev in admissible_ev}
    for cand in candidate_interpretations:
        if cand.get("relation") in ("supports", "limits", "contradicts"):
            sanitized_candidates.append({
                "rule_id": "R_EVIDENCE_MAPPING",
                "relation": cand["relation"],
                "cited_evidence_ids": sorted(allowed_ids.intersection(cand.get("cited_evidence_ids", []))),
            })
    rng.shuffle(sanitized_candidates)
    for index, candidate in enumerate(sanitized_candidates, 1):
        candidate["candidate_id"] = f"candidate_{index}"
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


def _model_evidence(ev: EvidenceRecord) -> Dict[str, Any]:
    """Send numeric measurements and safe column IDs; group labels may be untrusted CSV cells."""
    fields = {
        "target_col_id", "group_col_id", "stratify_col_id", "metric", "method", "row_count", "col_count",
        "treated_n", "control_n", "treated_stat", "control_stat", "diff", "p_value", "ci_95_low",
        "ci_95_high", "baseline_diff", "adjusted_diff", "relative_change_ratio", "value",
    }
    result = {key: ev.result[key] for key in fields if key in ev.result}
    if isinstance(ev.result.get("strata"), list):
        result["strata"] = [{key: row[key] for key in fields if key in row} for row in ev.result["strata"]]
    return {"evidence_id": ev.evidence_id, "tool_id": ev.tool_id, "scope_id": ev.scope_id,
            "sample_size": ev.sample_size, "result": result}


def _judge_payload(context: BlindedJudgeInput) -> str:
    data = context.model_dump(mode="json")
    # The frozen structured claim and receipt IDs suffice for a mapping correction.
    data["claim_spec"].pop("raw_text")
    data["claim_spec"].pop("context_notes")
    data["admissible_evidence"] = [_model_evidence(ev) for ev in context.admissible_evidence]
    return json.dumps(data, ensure_ascii=False)


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
        self.output_root = sandbox_root(output_root)
        # Older callers inject an engine to configure read roots. Execution always uses stdio.
        self.registry = stat_engine.registry if stat_engine else build_default_registry()
        self.final_judge_enabled = final_judge_enabled
        self.max_tool_calls = max_tool_calls
        self._blind_review_fn = blind_review_fn
        self._cross_review_fn = cross_review_fn
        self._final_judge_fn = final_judge_fn

    @staticmethod
    def _parameters(task: VerificationTask, run_id: str) -> Dict[str, Any]:
        params = dict(task.parameters)
        if "claim_id" in params or ("run_id" in params and task.tool_id != "get_evidence"):
            raise ValueError("run_id and claim_id are assigned by the workflow")
        if task.tool_id == "get_evidence":
            if params.setdefault("run_id", run_id) != run_id:
                raise ValueError("Evidence lookup outside this run")
            params.setdefault("evidence_id", None)
        elif task.tool_id == "aggregate":
            params.setdefault("filters", None)
            if params["filters"] == {}:
                params["filters"] = None
        elif task.tool_id == "group_compare":
            params.setdefault("metric", "mean")
            params.setdefault("stratify_col_id", None)
        elif task.tool_id == "sensitivity_check":
            params.setdefault("method", "winsorize_5pct")
        return params

    async def _run_pending_mcp(
        self, state: WorkflowState, trace: List[Dict[str, Any]],
        client: StatMCPClient, attempted: set[str],
    ) -> None:
        if state.status is not None:
            return

        state.new_information = False
        while state.pending_tests:
            task = state.pending_tests[0]
            try:
                params = self._parameters(task, state.run_id)
                key = compute_request_key(state.dataset_hash, task.tool_id, task.tool_version, params)
                if key in attempted:
                    trace.append({"stage": "PLAN_AND_RUN_MCP", "event": "cache_hit", "request_key": key})
                    state.pending_tests.pop(0)
                    continue
                if state.remaining_tool_calls <= 0:
                    state.stop_reason = "TOOL_CALL_LIMIT_REACHED"
                    break
                attempted.add(key)
                state.remaining_tool_calls -= 1
                if task.tool_id == "get_evidence":
                    records = await client.call_tool(task.tool_id, params)
                    # Ledger reads can only corroborate evidence admitted earlier in this run.
                    expected = state.evidence
                    if params["evidence_id"] is not None:
                        eid = params["evidence_id"]
                        expected = {eid: state.evidence[eid]}
                    if not isinstance(records, list) or len(records) != len(expected):
                        raise ValueError("Unexpected ledger response")
                    fetched = [EvidenceRecord.model_validate(item) for item in records]
                    if {ev.evidence_id: ev for ev in fetched} != expected:
                        raise ValueError("Ledger mismatch")
                    trace.append({"stage": "PLAN_AND_RUN_MCP", "event": "ledger_read",
                                  "tool_id": task.tool_id, "count": len(fetched)})
                else:
                    raw = await client.call_tool(task.tool_id, {**params, "claim_id": state.claim_id})
                    rec = EvidenceRecord.model_validate(raw)
                    digest = hashlib.sha256(canonical_json(rec.result).encode("utf-8")).hexdigest()
                    if (rec.claim_id != state.claim_id or rec.tool_id != task.tool_id
                            or rec.dataset_hash != state.dataset_hash or rec.tool_version != task.tool_version
                            or rec.request_hash != key or rec.result_hash != digest
                            or rec.execution_status != "ok" or not rec.applicable
                            or rec.evidence_id in state.evidence):
                        raise ValueError("Evidence does not match the frozen task")
                    state.evidence[rec.evidence_id] = rec
                    state.executed_tests[key] = rec.evidence_id
                    state.new_information = True
                    trace.append({"stage": "PLAN_AND_RUN_MCP", "event": "tool_executed",
                                  "tool_id": task.tool_id, "evidence_id": rec.evidence_id})
            except (ValueError, KeyError, RuntimeError, TypeError, SecurityError):
                state.critical_gap = True
                trace.append({"stage": "PLAN_AND_RUN_MCP", "event": "tool_rejected_or_failed",
                              "tool_id": task.tool_id})
            state.pending_tests.pop(0)

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
        validate_run_id(assigned_run_id)
        if initial_conflict_override and (
            not self.router.mock_mode or any(
                key not in _MOCK_CONFLICT_FIELDS or val != _MOCK_CONFLICT_FIELDS[key]
                for key, val in initial_conflict_override.items()
            )
        ):
            raise ValueError("Unsupported conflict fixture")
        trace: List[Dict[str, Any]] = []

        if budget_cny is not None:
            if not math.isfinite(budget_cny) or budget_cny < 0:
                raise ValueError("budget_cny must be finite and non-negative")
            self.router.max_budget_cny = min(self.router.max_budget_cny, budget_cny)

        # Reserve the namespace once. Both the server ledger and reports belong to this invocation.
        self.output_root.mkdir(parents=True, exist_ok=True)
        sandbox_root(self.output_root / assigned_run_id).mkdir(exist_ok=False)

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
                stop_reason="INIT_INVALID_INPUT",
                remaining_budget=self.router.remaining_budget_cny,
                remaining_tool_calls=self.max_tool_calls,
            )
            return self._finalize(empty_claim, state, trace, persist_outputs=persist_outputs)

        profile = self.registry.register_csv(data_path)
        claim_id = f"claim_{assigned_run_id[-6:]}"
        try:
            frozen_claim = claim_spec or freeze_claim(raw_claim, claim_id=claim_id, context_notes=context_notes,
                                                       router=self.router)
        except BudgetExceededError:
            placeholder = ClaimSpec(claim_id=claim_id, raw_text=raw_claim, claim_type="descriptive", metric="unknown")
            stopped = WorkflowState(run_id=assigned_run_id, claim_id=claim_id, dataset_hash=profile.dataset_hash,
                                    critical_gap=True, stop_reason="BUDGET_EXCEEDED",
                                    remaining_tool_calls=self.max_tool_calls)
            trace.append({"stage": "INIT", "event": "budget_exceeded"})
            return self._finalize(placeholder, stopped, trace, persist_outputs=persist_outputs)
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
            if (p_out.claim_id != state.claim_id or c_out.claim_id != state.claim_id
                    or any(issue.target_claim_id not in {state.claim_id, *(s.sub_claim_id for s in frozen_claim.sub_claims)}
                           for issue in issues_map.values())):
                raise ValueError("Review references a different claim")
            state.critical_gap = any(
                issue.requested_test and issue.requested_test.tool_id not in ALLOWED_MCP_TOOLS
                for issue in issues_map.values()
            )
            # Opposite readings of the same objective test can be revisited after collection.
            disputed_issue = None
            if {p_out.position, c_out.position} == {ReviewPosition.SUPPORT, ReviewPosition.OPPOSE}:
                common = {canonical_json(i.requested_test) for i in p_out.issues if i.requested_test} & {
                    canonical_json(i.requested_test) for i in c_out.issues if i.requested_test
                }
                disputed_issue = next((key for key, issue in issues_map.items()
                                       if canonical_json(issue.requested_test) in common), None)
                if disputed_issue:
                    state.unresolved_conflict = True
                    state.conflict_type = "evidence_mapping"
                    state.conflict_is_material = True
                    state.verification_path_exists = len(common) == 1

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

        client = StatMCPClient(
            data_path=data_path, dataset_id=profile.dataset_id, dataset_hash=profile.dataset_hash,
            allowed_roots=self.registry.allowed_roots, run_id=assigned_run_id, output_root=self.output_root,
        )
        state = asyncio.run(self._run_reviews(
            client, profile, frozen_claim, state, trace, initial_conflict_override, disputed_issue
        ))
        trace.extend({"stage": "MCP_STDIO", **event} for event in client.timings)
        return self._finalize(frozen_claim, state, trace, persist_outputs=persist_outputs)

    async def _run_reviews(
        self, client: StatMCPClient, profile: DatasetProfile, frozen_claim: ClaimSpec,
        state: WorkflowState, trace: List[Dict[str, Any]], initial_conflict_override: Optional[Dict[str, Any]],
        disputed_issue: Optional[str],
    ) -> WorkflowState:
        async with client:
            await client.list_tools()
            return await self._bounded_loop(client, profile, frozen_claim, state, trace, initial_conflict_override, disputed_issue)

    async def _bounded_loop(
        self, client: StatMCPClient, profile: DatasetProfile, frozen_claim: ClaimSpec,
        state: WorkflowState, trace: List[Dict[str, Any]], initial_conflict_override: Optional[Dict[str, Any]],
        disputed_issue: Optional[str],
    ) -> WorkflowState:
        # ------------------------------------------------------------------
        # Stage 3 -> 4 -> (5) Bounded Loop
        # ------------------------------------------------------------------
        attempted: set[str] = set()
        max_iterations = 8
        for _ in range(max_iterations):
            if state.pending_tests:
                await self._run_pending_mcp(state, trace, client, attempted)

            # Stage 4: RULE_CHECK
            state = evaluate_evidence_rules(frozen_claim, state, profile=profile)
            state.remaining_budget = self.router.remaining_budget_cny

            if initial_conflict_override:
                for key in initial_conflict_override.keys() - {"material_limitation", "full_claim_supported"}:
                    setattr(state, key, _MOCK_CONFLICT_FIELDS[key])
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
            if state.required_checks_complete and state.full_claim_supported and not state.material_limitation and not state.unresolved_conflict:
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
                    deltas = self._cross_review_fn(frozen_claim.model_copy(deep=True), state.model_copy(deep=True))
                elif not self.router.mock_mode:
                    snapshot = {"claim": frozen_claim.model_dump(mode="json"), "dataset_id": profile.dataset_id,
                                "evidence": [_model_evidence(ev) for ev in state.evidence.values()]}
                    prompt = ("Return one JSON incremental action using this schema. Use only the five "
                              "statistical tools from the first review and existing evidence IDs. "
                              + json.dumps(CrossReviewDelta.model_json_schema()))
                    deltas = []
                    try:
                        for role in ("primary_reviewer", "challenger"):
                            reply = self.router.complete(role, [
                                {"role": "system", "content": prompt},
                                {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False)},
                            ], expect_json=True)
                            delta = CrossReviewDelta.model_validate(reply.parsed_json)
                            if delta.claim_id != frozen_claim.claim_id:
                                raise ModelCallError("Cross review references another claim")
                            deltas.append((role, delta))
                    except BudgetExceededError:
                        state.stop_reason = "BUDGET_EXCEEDED"
                        state.review_closed = True
                        break
                    finally:
                        state.model_diversity_lost |= self.router.model_diversity_lost
                        state.remaining_budget = self.router.remaining_budget_cny
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
                    executed_request_keys=attempted,
                    existing_evidence_ids=set(state.evidence.keys()),
                )
                state.review_closed = True
                state.cheap_resolution_exhausted = True
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

                unresolved_issue_id = disputed_issue or next(iter(state.issues), "iss_mapping")
                issue = state.issues.get(unresolved_issue_id)
                cited_ids = []
                if issue and issue.requested_test:
                    task = VerificationTask(task_id="mapping", dataset_hash=state.dataset_hash,
                                            **issue.requested_test.model_dump())
                    key = compute_request_key(state.dataset_hash, task.tool_id, task.tool_version,
                                              self._parameters(task, state.run_id))
                    if key in state.executed_tests:
                        cited_ids.append(state.executed_tests[key])
                blinded_input = build_blinded_judge_input(
                    claim_spec=frozen_claim,
                    state=state,
                    unresolved_issue_id=unresolved_issue_id,
                    candidate_interpretations=[
                        {"relation": relation, "rule_id": "R_EVIDENCE_MAPPING", "cited_evidence_ids": cited_ids}
                        for relation in ("supports", "limits", "contradicts")
                    ],
                )

                try:
                    if self._final_judge_fn is not None:
                        raw_judge_out = self._final_judge_fn(blinded_input, self.router)
                    else:
                        judge_prompt = load_prompt_template("final_judge")
                        resp = self.router.complete(
                            role="final_reviewer",
                            messages=[
                                {"role": "system", "content": judge_prompt},
                                {"role": "user", "content": _judge_payload(blinded_input)},
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

                except BudgetExceededError:
                    state.stop_reason = "BUDGET_EXCEEDED"
                    break
                except ModelCallError:
                    state.stop_reason = "JUDGE_UNAVAILABLE"
                    break

                is_valid, parsed_out, reject_reasons = validate_judge_output(raw_judge_out, blinded_input)
                trace.append(
                    {
                        "stage": "FINAL_JUDGE",
                        "valid": is_valid,
                        "reject_reasons": reject_reasons,
                    }
                )
                if is_valid and isinstance(parsed_out, JudgeMappingCorrection):
                    if issue is not None:
                        issue.evidence_ids = list(parsed_out.evidence_ids)
                        issue.status = IssueStatus.RESOLVED
                    state.unresolved_conflict = False
                    state.stop_reason = f"JUDGE_MAPPING_VERIFIED:{parsed_out.candidate_id}"
                    continue
                if is_valid and isinstance(parsed_out, JudgeUnresolvedReport):
                    state.unresolved_conflict = True
                    state.stop_reason = "JUDGE_REPORTED_UNRESOLVED"
                    break

                # 机械校验拒绝越权或伪造证据 ID -> 绝不采纳，绝不覆盖硬规则
                state.unresolved_conflict = True
                state.stop_reason = "JUDGE_OUTPUT_REJECTED"
                break

            break

        return state

    def _finalize(
        self,
        claim_spec: ClaimSpec,
        state: WorkflowState,
        trace: List[Dict[str, Any]],
        *,
        persist_outputs: bool,
    ) -> Dict[str, Any]:
        """Stage 6: FINALIZE — 纯确定性 `classify(state)` 与审计报告落盘。"""
        state.remaining_budget = self.router.remaining_budget_cny
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
            out_dir = sandbox_root(self.output_root / state.run_id)
            report_file = resolve_safe_path(out_dir / "report.md", [out_dir])
            verdict_file = resolve_safe_path(out_dir / "verdict.json", [out_dir])
            trace_file = resolve_safe_path(out_dir / "trace.json", [out_dir])
            out_dir.mkdir(parents=True, exist_ok=True)

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
