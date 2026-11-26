r"""Skill 4: 确定性规则裁决与证据链核验报告（`skill_4_evidence_auditor`）。

职责（严格遵循三权分立——裁决权完全归确定性规则引擎，禁止模型覆盖硬规则）：
1. 灰色证据与稳健性相对变化率判定：$D = \frac{|\beta_1 - \beta_0|}{\max(|\beta_0|, \epsilon)}$（默认阈值 `0.20 / 0.50 / 0.05`）。
2. 确定性规则核验（`evaluate_evidence_rules`）与复合命题合取保护（防止方向通过被误判为因果 `verified`）。
3. 纯确定性最终裁决函数 `classify(state)`（`verified` | `limited` | `contradicted` | `insufficient`）。
4. 终裁模型输出机械校验 `validate_judge_output(raw_output, context)`（拦截不存在 `evidence_id`、`final_status` 越权与非法候选映射）。
5. 绑定 `evidence_id` 的可追溯 Markdown 审计报告生成（`render_markdown_report`）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import ValidationError

from core.schema import (
    AuditVerdict,
    BlindedJudgeInput,
    ClaimSpec,
    ClaimType,
    EvidenceRecord,
    FinalJudgeOutput,
    FinalJudgeOutputAdapter,
    IssueStatus,
    JudgeMappingCorrection,
    VerdictStatus,
    WorkflowState,
)

_FORBIDDEN_MAPPING_KEYS: Set[str] = {
    "final_status",
    "status",
    "override_rule",
    "override_hard_rule",
    "decisive_refutation",
}


def compute_relative_change_ratio(
    baseline: float,
    adjusted: float,
    eps: float = 1e-9,
) -> float:
    """计算稳健性相对变化率 D = |beta_1 - beta_0| / max(|beta_0|, eps)。"""
    return abs(adjusted - baseline) / max(abs(baseline), eps)


def assess_robustness(
    baseline: float,
    adjusted: float,
    *,
    warning_threshold: float = 0.20,
    severe_threshold: float = 0.50,
) -> Dict[str, Any]:
    """评估缩尾/去极值前后的稳健性变化等级。"""
    ratio = compute_relative_change_ratio(baseline, adjusted)
    sign_reversed = (baseline > 0 and adjusted < 0) or (baseline < 0 and adjusted > 0)
    if ratio > severe_threshold:
        severity = "severe"
    elif ratio > warning_threshold:
        severity = "warning"
    else:
        severity = "stable"
    return {
        "baseline": baseline,
        "adjusted": adjusted,
        "relative_change_ratio": round(ratio, 6),
        "severity": severity,
        "material_limitation": severity in ("warning", "severe"),
        "sign_reversed": sign_reversed,
    }


def _direction_matches(value: float, direction: Optional[str]) -> bool:
    if direction == "increase":
        return value > 0
    if direction == "decrease":
        return value < 0
    return True


def _is_causal_claim(claim_spec: ClaimSpec) -> bool:
    if claim_spec.claim_type == ClaimType.CAUSAL:
        return True
    return any(sc.required and sc.claim_type == ClaimType.CAUSAL for sc in claim_spec.sub_claims)


def _is_universal_claim(claim_spec: ClaimSpec) -> bool:
    if claim_spec.claim_type == ClaimType.UNIVERSAL:
        return True
    return any(sc.required and sc.claim_type == ClaimType.UNIVERSAL for sc in claim_spec.sub_claims)


def evaluate_evidence_rules(
    claim_spec: ClaimSpec,
    state: WorkflowState,
    *,
    warning_threshold: float = 0.20,
    severe_threshold: float = 0.50,
    significance_alpha: float = 0.05,
    magnitude_rel_tol: float = 0.15,
) -> WorkflowState:
    """基于账本中有效 `EvidenceRecord` 与 `IssueItem` 确定性更新 `WorkflowState` 布尔位。

    不直接修改输入对象，返回更新后的新 `WorkflowState` 副本。
    """
    next_state = state.model_copy(deep=True)
    admissible: List[EvidenceRecord] = [
        ev
        for ev in next_state.evidence.values()
        if ev.execution_status == "ok" and ev.applicable
    ]

    # 检查是否存在不可补齐的关键数据/因果缺口
    has_unresolvable_gap = any(
        iss.status == IssueStatus.UNRESOLVABLE
        and iss.issue_type in ("missing_field", "causal_gap", "unidentifiable")
        for iss in next_state.issues.values()
    )
    if has_unresolvable_gap:
        next_state.critical_gap = True

    if not admissible:
        next_state.required_checks_complete = False
        return next_state

    core_supported = False
    magnitude_supported = True
    decisive_refutation = next_state.decisive_refutation
    material_limitation = next_state.material_limitation
    has_causal_control_evidence = False

    for ev in admissible:
        res = ev.result

        # 1. 分组比较 / 分层混淆 / 子组反例检验
        if ev.tool_id == "group_compare":
            raw_diff = res.get("diff")
            p_value = res.get("p_value")
            strata = res.get("strata")

            if strata and isinstance(strata, list):
                has_causal_control_evidence = True
                strata_diffs = [
                    float(s["diff"])
                    for s in strata
                    if isinstance(s, dict) and s.get("diff") is not None
                ]
                if strata_diffs:
                    # 全称命题存在可靠负向子组反例 -> 决定性反证
                    if _is_universal_claim(claim_spec) and any(
                        not _direction_matches(d, claim_spec.direction) for d in strata_diffs
                    ):
                        decisive_refutation = True

                    # 辛普森悖论：所有分层差值均归零或反向，推翻原始正向/幅度/因果声明
                    all_collapsed = all(abs(d) <= 1e-6 for d in strata_diffs)
                    all_reversed = all(
                        not _direction_matches(d, claim_spec.direction) for d in strata_diffs
                    )
                    if (all_collapsed or all_reversed) and claim_spec.direction in ("increase", "decrease"):
                        decisive_refutation = True
            elif raw_diff is not None:
                diff_val = float(raw_diff)
                dir_ok = _direction_matches(diff_val, claim_spec.direction)
                sig_ok = p_value is None or float(p_value) <= significance_alpha
                if dir_ok and sig_ok:
                    core_supported = True
                elif not dir_ok:
                    decisive_refutation = True

                # 幅度断言误差核验
                if claim_spec.target_value is not None and claim_spec.claim_type in (
                    ClaimType.MAGNITUDE,
                    ClaimType.CAUSAL,
                ):
                    target = float(claim_spec.target_value)
                    rel_err = abs(diff_val - target) / max(abs(target), 1e-9)
                    if rel_err > magnitude_rel_tol:
                        magnitude_supported = False
                        decisive_refutation = True

        # 2. 聚合口径与分母增长率检验
        elif ev.tool_id == "aggregate":
            observed = res.get("growth_rate_pct")
            if observed is None:
                observed = res.get("value")
            if observed is not None and claim_spec.target_value is not None:
                obs_val = float(observed)
                target = float(claim_spec.target_value)
                rel_err = abs(obs_val - target) / max(abs(target), 1e-9)
                if rel_err <= magnitude_rel_tol:
                    core_supported = True
                else:
                    magnitude_supported = False
                    decisive_refutation = True
            elif observed is not None:
                core_supported = True

        # 3. 稳健性 / 异常值敏感性检验
        elif ev.tool_id == "sensitivity_check":
            baseline = res.get("baseline_diff")
            adjusted = res.get("adjusted_diff")
            if baseline is not None and adjusted is not None:
                rob = assess_robustness(
                    float(baseline),
                    float(adjusted),
                    warning_threshold=warning_threshold,
                    severe_threshold=severe_threshold,
                )
                if rob["sign_reversed"]:
                    decisive_refutation = True
                elif rob["material_limitation"]:
                    material_limitation = True
                    # 方向命题在缩尾后方向仍成立 -> 核心方向支持但存在重大局限 (limited)
                    if _direction_matches(float(adjusted), claim_spec.direction):
                        core_supported = True
                    # 严格幅度断言在缩尾后严重衰减超过 50% -> 完整幅度不成立
                    if claim_spec.claim_type == ClaimType.MAGNITUDE and rob["severity"] == "severe":
                        magnitude_supported = False

    # 复合命题合取保护：因果命题若缺少因果识别/分层控制证据，不可将方向通过误判为整体 verified
    if _is_causal_claim(claim_spec) and not has_causal_control_evidence and not decisive_refutation:
        next_state.critical_gap = True

    next_state.required_checks_complete = True
    next_state.core_supported = core_supported
    next_state.decisive_refutation = decisive_refutation
    next_state.material_limitation = material_limitation
    next_state.full_claim_supported = (
        core_supported
        and magnitude_supported
        and not decisive_refutation
        and not material_limitation
        and not next_state.critical_gap
    )
    return next_state


def classify(state: WorkflowState) -> VerdictStatus:
    """纯确定性最终裁决函数（严格按 V2 蓝图优先级）。

    优先级：
    1. 有效决定性反证 -> `contradicted`
    2. 关键缺口 / 未决冲突 / 必要检验未完 -> `insufficient`
    3. 完整支持且无重大局限 -> `verified`
    4. 核心支持但存在重大局限 -> `limited`
    5. 默认保守兜底 -> `insufficient`
    """
    if state.decisive_refutation:
        return VerdictStatus.CONTRADICTED
    if state.critical_gap or state.unresolved_conflict or not state.required_checks_complete:
        return VerdictStatus.INSUFFICIENT
    if state.full_claim_supported and not state.material_limitation:
        return VerdictStatus.VERIFIED
    if state.core_supported and state.material_limitation:
        return VerdictStatus.LIMITED
    return VerdictStatus.INSUFFICIENT


def check_invariants(state: WorkflowState) -> Tuple[bool, List[str]]:
    """检查 6 条全局强制不变量，返回 `(all_passed, violations)`。"""
    violations: List[str] = []
    if state.round > 2:
        violations.append(f"round={state.round} exceeds limit 2")
    if state.judge_calls > 1:
        violations.append(f"judge_calls={state.judge_calls} exceeds limit 1")
    if state.duplicate_tool_executions != 0:
        violations.append(f"duplicate_tool_executions={state.duplicate_tool_executions} != 0")
    if state.invalid_evidence_accepted != 0:
        violations.append(f"invalid_evidence_accepted={state.invalid_evidence_accepted} != 0")
    if state.judge_rule_overrides != 0:
        violations.append(f"judge_rule_overrides={state.judge_rule_overrides} != 0")
    if state.tool_calls_after_terminal != 0:
        violations.append(f"tool_calls_after_terminal={state.tool_calls_after_terminal} != 0")
    return len(violations) == 0, violations


def validate_judge_output(
    raw_output: Any,
    context: BlindedJudgeInput,
) -> Tuple[bool, Optional[FinalJudgeOutput], List[str]]:
    """机械核验终裁模型输出：拦截越权字段、不存在的 `evidence_id` 与非法候选 ID。"""
    reasons: List[str] = []

    if isinstance(raw_output, dict) and any(k in raw_output for k in _FORBIDDEN_MAPPING_KEYS):
        reasons.append("Judge output attempted forbidden rule/status override field")
        return False, None, reasons

    try:
        parsed: FinalJudgeOutput = FinalJudgeOutputAdapter.validate_python(raw_output)
    except ValidationError as exc:
        reasons.append(f"Schema validation failed: {exc.errors()[0]['msg']}")
        return False, None, reasons

    if parsed.claim_id != context.claim_id:
        reasons.append(f"claim_id mismatch: got '{parsed.claim_id}', expected '{context.claim_id}'")
    if parsed.issue_id != context.unresolved_issue_id:
        reasons.append(
            f"issue_id mismatch: got '{parsed.issue_id}', expected '{context.unresolved_issue_id}'"
        )

    admissible_ids: Set[str] = {
        ev.evidence_id
        for ev in context.admissible_evidence
        if ev.execution_status == "ok" and ev.applicable
    }
    valid_candidate_ids: Set[str] = {
        str(c.get("candidate_id") or c.get("id"))
        for c in context.candidate_interpretations
        if isinstance(c, dict) and (c.get("candidate_id") or c.get("id"))
    }

    if isinstance(parsed, JudgeMappingCorrection):
        if valid_candidate_ids and parsed.candidate_id not in valid_candidate_ids:
            reasons.append(f"Unknown candidate_id: '{parsed.candidate_id}'")
        if not parsed.evidence_ids:
            reasons.append("Mapping correction must cite at least one admissible evidence_id")
        unknown_ev = [eid for eid in parsed.evidence_ids if eid not in admissible_ids]
        if unknown_ev:
            reasons.append(f"Cited non-existent or inadmissible evidence_ids: {unknown_ev}")
        forbidden_in_map = [k for k in parsed.proposed_mapping if k in _FORBIDDEN_MAPPING_KEYS]
        if forbidden_in_map:
            reasons.append(f"proposed_mapping contains forbidden override keys: {forbidden_in_map}")

    if reasons:
        return False, None, reasons
    return True, parsed, []


def build_audit_verdict(
    claim_spec: ClaimSpec,
    state: WorkflowState,
    *,
    revised_claim: Optional[str] = None,
) -> AuditVerdict:
    """汇总确定性裁决与有效证据 ID，输出冻结契约 `AuditVerdict`。"""
    status = classify(state)
    invariants_ok, violations = check_invariants(state)

    admissible_evs = [
        ev for ev in state.evidence.values() if ev.execution_status == "ok" and ev.applicable
    ]
    cited_ids = sorted(ev.evidence_id for ev in admissible_evs)

    key_findings: List[str] = []
    limitations: List[str] = []
    for ev in admissible_evs:
        key_findings.append(
            f"[{ev.evidence_id}] `{ev.tool_id}` ({ev.scope_id}, n={ev.sample_size}): {ev.result}"
        )
        for lim in ev.limitations:
            limitations.append(f"[{ev.evidence_id}] {lim}")

    if state.model_diversity_lost:
        limitations.append("双方主力模型同时降级为同一备用模型（model_diversity_lost=True），异构辩论多样性受损。")
    if state.critical_gap:
        limitations.append("存在未补齐的关键字段或因果识别缺口，拒绝越权外推结论。")
    if not invariants_ok:
        limitations.extend(f"不变量告警：{v}" for v in violations)

    return AuditVerdict(
        claim_id=claim_spec.claim_id,
        status=status,
        cited_evidence_ids=cited_ids,
        key_findings=key_findings,
        limitations=limitations,
        revised_claim=revised_claim,
        stop_reason=state.stop_reason or f"RULE_FINALIZE:{status.value}",
        invariants_passed=invariants_ok,
    )


def render_markdown_report(
    claim_spec: ClaimSpec,
    verdict: AuditVerdict,
    state: WorkflowState,
) -> str:
    """生成绑定 `evidence_id` 的确定性 Markdown 审计报告。"""
    invariants_ok, violations = check_invariants(state)
    lines: List[str] = [
        f"# 《数证 Audit》证据链核验报告 — `{claim_spec.claim_id}`",
        "",
        f"- **最终确定性裁决**：`{verdict.status.value.upper()}`",
        f"- **待审原始声明**：{claim_spec.raw_text}",
        f"- **命题类型**：`{claim_spec.claim_type.value}`（指标：`{claim_spec.metric}`）",
        f"- **终止原因**：`{verdict.stop_reason}`",
        f"- **6 条全局不变量**：{'全部通过' if invariants_ok else '存在违背：' + '; '.join(violations)}",
        "",
        "## 一、可追溯证据核验明细",
    ]
    if verdict.key_findings:
        for finding in verdict.key_findings:
            lines.append(f"- {finding}")
    else:
        lines.append("- （无有效可采纳证据记录）")

    lines.append("")
    lines.append("## 二、局限性与风险提示")
    if verdict.limitations:
        for lim in verdict.limitations:
            lines.append(f"- {lim}")
    else:
        lines.append("- 无显著统计局限或安全异常。")

    if verdict.revised_claim:
        lines.extend(["", "## 三、建议修正表述", f"> {verdict.revised_claim}"])

    lines.extend(
        [
            "",
            "## 四、运行不变量审计",
            "| 不变量 | 当前值 | 约束 |",
            "| :--- | :--- | :--- |",
            f"| `round` | {state.round} | `<= 2` |",
            f"| `judge_calls` | {state.judge_calls} | `<= 1` |",
            f"| `duplicate_tool_executions` | {state.duplicate_tool_executions} | `== 0` |",
            f"| `invalid_evidence_accepted` | {state.invalid_evidence_accepted} | `== 0` |",
            f"| `judge_rule_overrides` | {state.judge_rule_overrides} | `== 0` |",
            f"| `tool_calls_after_terminal` | {state.tool_calls_after_terminal} | `== 0` |",
            "",
        ]
    )
    return "\n".join(lines)
