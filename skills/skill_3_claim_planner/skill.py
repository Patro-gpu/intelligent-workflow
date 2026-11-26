"""Skill 3: 命题冻结与双轮增量验证规划（`skill_3_claim_planner`）。

职责（严格遵循三权分立——仅拥有辩论与规划权，无裁决权）：
1. 命题冻结：将自然语言声明解析为严格符合 `ClaimSpec` 的结构化命题（5 类 `ClaimType`，最多 1 主 + 2 子命题）。
2. 第 1 轮并行盲审：`primary_reviewer` 与 `challenger` 各自仅见 `ClaimSpec` 与脱敏 `DatasetProfile`，独立输出 `BlindReviewOutput`。
3. 确定性去重与任务规划：基于 `compute_issue_key` 与 `compute_request_key` 合并争议与白名单检验任务，杜绝重复执行。
4. 第 2 轮增量验证规划：仅接受 5 种 `ReviewAction` 枚举，拦截复读、重复检验与无证据妥协，输出 `NO_PROGRESS` 停止信号。
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from core.llm_router import LLMRouter
from core.schema import (
    BlindReviewOutput,
    ClaimSpec,
    ClaimType,
    CrossReviewDelta,
    DatasetProfile,
    IssueItem,
    IssueStatus,
    ReviewAction,
    ReviewPosition,
    SubClaim,
    ToolRequest,
    VerificationTask,
    compute_issue_key,
    compute_request_key,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = REPO_ROOT / "configs/prompts"

ALLOWED_MCP_TOOLS: Set[str] = {
    "inspect_data",
    "aggregate",
    "group_compare",
    "sensitivity_check",
    "get_evidence",
}

_CAUSAL_KEYWORDS = ("使", "导致", "带来", "因果", "净效应", "造成", "caused", "cause", "effect")
_UNIVERSAL_KEYWORDS = ("所有", "全部", "普遍", "每一", "无一例外", "均高于", "all ", "every", "universal")
_DIRECTION_INC_KEYWORDS = ("高于", "增加", "增长", "提升", "上升", "高出", "大于", "increase", "higher", "more")
_DIRECTION_DEC_KEYWORDS = ("低于", "减少", "下降", "降低", "小于", "decrease", "lower", "less")
_CONFOUNDER_CONTEXT_HINTS = ("大促", "促销", "活动", "混淆", "promo", "confound")
_NUMBER_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(%|％|单|万元|万|元|次|件)?")


def load_prompt_template(name: str) -> str:
    """加载 `configs/prompts/<name>.md` 外置提示词模板。"""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def _extract_numeric_target(text: str) -> Tuple[Optional[float], Optional[str]]:
    match = _NUMBER_RE.search(text)
    if not match:
        return None, None
    val = float(match.group(1))
    raw_unit = match.group(2)
    unit_map = {
        "%": "percent",
        "％": "percent",
        "单": "orders",
        "万元": "wan_cny",
        "万": "wan",
        "元": "cny",
    }
    return val, unit_map.get(raw_unit, raw_unit)


def _infer_direction(text: str) -> Optional[str]:
    lowered = text.lower()
    if any(k in lowered for k in _DIRECTION_INC_KEYWORDS):
        return "increase"
    if any(k in lowered for k in _DIRECTION_DEC_KEYWORDS):
        return "decrease"
    return None


def _infer_claim_type(text: str, target_value: Optional[float], direction: Optional[str]) -> ClaimType:
    lowered = text.lower()
    if any(k in lowered for k in _CAUSAL_KEYWORDS):
        return ClaimType.CAUSAL
    if any(k in lowered for k in _UNIVERSAL_KEYWORDS):
        return ClaimType.UNIVERSAL
    if target_value is not None:
        return ClaimType.MAGNITUDE
    if direction is not None:
        return ClaimType.DIRECTION
    return ClaimType.DESCRIPTIVE


def _infer_metric_name(text: str, unit: Optional[str]) -> str:
    if "增长率" in text or unit == "percent":
        return "growth_rate"
    if "营收" in text or "收入" in text:
        return "revenue"
    if "订单" in text or unit == "orders":
        return "orders_mean_diff"
    return "target_metric"


def freeze_claim(
    raw_text: str,
    *,
    claim_id: str = "claim_01",
    context_notes: str = "",
    metric: Optional[str] = None,
    scope_id: str = "global",
    time_window: Optional[str] = None,
    router: Optional[LLMRouter] = None,
) -> ClaimSpec:
    """将自然语言业务声明冻结为严格结构化 `ClaimSpec`（最多 1 主 + 2 子命题）。"""
    cleaned = raw_text.strip()
    if not cleaned:
        raise ValueError("raw_text must not be empty")

    # 真实在线模式下优先尝试由模型按外置模板解析并经 Pydantic 严格校验
    if router is not None and not router.mock_mode:
        prompt = load_prompt_template("claim_parser")
        resp = router.complete(
            role="primary_reviewer",
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": f"claim_id={claim_id}\nraw_text={cleaned}\ncontext_notes={context_notes}",
                },
            ],
            expect_json=True,
        )
        if resp.parsed_json:
            return ClaimSpec.model_validate(resp.parsed_json)

    target_value, unit = _extract_numeric_target(cleaned)
    direction = _infer_direction(cleaned)
    claim_type = _infer_claim_type(cleaned, target_value, direction)
    resolved_metric = metric or _infer_metric_name(cleaned, unit)

    sub_claims: List[SubClaim] = []
    if claim_type == ClaimType.CAUSAL:
        if target_value is not None:
            sub_claims.append(
                SubClaim(
                    sub_claim_id=f"{claim_id}_sub_magnitude",
                    claim_type=ClaimType.MAGNITUDE,
                    metric=resolved_metric,
                    direction=direction,
                    target_value=target_value,
                    unit=unit,
                    scope_id=scope_id,
                    required=True,
                )
            )
        elif direction is not None:
            sub_claims.append(
                SubClaim(
                    sub_claim_id=f"{claim_id}_sub_direction",
                    claim_type=ClaimType.DIRECTION,
                    metric=resolved_metric,
                    direction=direction,
                    unit=unit,
                    scope_id=scope_id,
                    required=True,
                )
            )
        sub_claims.append(
            SubClaim(
                sub_claim_id=f"{claim_id}_sub_causal",
                claim_type=ClaimType.CAUSAL,
                metric=resolved_metric,
                direction=direction,
                target_value=target_value,
                unit=unit,
                scope_id=scope_id,
                required=True,
            )
        )
    elif claim_type == ClaimType.UNIVERSAL:
        sub_claims.append(
            SubClaim(
                sub_claim_id=f"{claim_id}_sub_overall",
                claim_type=ClaimType.DIRECTION if direction else ClaimType.MAGNITUDE,
                metric=resolved_metric,
                direction=direction,
                target_value=target_value,
                unit=unit,
                scope_id=scope_id,
                required=True,
            )
        )
        sub_claims.append(
            SubClaim(
                sub_claim_id=f"{claim_id}_sub_subgroup",
                claim_type=ClaimType.UNIVERSAL,
                metric=resolved_metric,
                direction=direction,
                unit=unit,
                scope_id=scope_id,
                required=True,
            )
        )

    return ClaimSpec(
        claim_id=claim_id,
        raw_text=cleaned,
        claim_type=claim_type,
        metric=resolved_metric,
        direction=direction,
        target_value=target_value,
        unit=unit,
        scope_id=scope_id,
        time_window=time_window,
        context_notes=context_notes,
        sub_claims=sub_claims[:2],
    )


def _pick_metric_and_group_cols(
    profile: DatasetProfile,
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """从脱敏 `DatasetProfile` 中按安全 `col_id` 识别指标列、主分组列与候选分层列。"""
    numeric_cols = [c for c in profile.columns if c.role_hint == "numeric_metric"]
    group_cols = [c for c in profile.columns if c.col_id in profile.candidate_group_cols]

    target_col_id = numeric_cols[-1].col_id if numeric_cols else None
    primary_group_id: Optional[str] = None
    stratify_candidates: List[str] = []

    for col in group_cols:
        lowered = col.sanitized_name.lower()
        if primary_group_id is None and any(k in lowered for k in ("coupon", "treat", "group")):
            primary_group_id = col.col_id
        else:
            stratify_candidates.append(col.col_id)

    if primary_group_id is None and group_cols:
        primary_group_id = group_cols[0].col_id
        stratify_candidates = [c.col_id for c in group_cols[1:]]

    return target_col_id, primary_group_id, stratify_candidates


def _build_mock_primary_review(claim: ClaimSpec, profile: DatasetProfile) -> BlindReviewOutput:
    target_col, group_col, _ = _pick_metric_and_group_cols(profile)
    issues: List[IssueItem] = []

    if target_col and group_col:
        issue_id = compute_issue_key(claim.claim_id, "metric_verification", claim.scope_id, "R_CORE", target_col)[:12]
        issues.append(
            IssueItem(
                issue_id=f"iss_{issue_id}",
                issue_type="metric_verification",
                target_claim_id=claim.claim_id,
                scope_id=claim.scope_id,
                rule_id="R_CORE",
                status=IssueStatus.OPEN,
                rationale="核验分组均值差是否支持原始声明的方向与幅度。",
                requested_test=ToolRequest(
                    tool_id="group_compare",
                    parameters={
                        "dataset_id": profile.dataset_id,
                        "target_col_id": target_col,
                        "group_col_id": group_col,
                        "metric": "mean",
                    },
                ),
            )
        )
    elif target_col:
        issue_id = compute_issue_key(claim.claim_id, "metric_verification", claim.scope_id, "R_AGG", target_col)[:12]
        issues.append(
            IssueItem(
                issue_id=f"iss_{issue_id}",
                issue_type="metric_verification",
                target_claim_id=claim.claim_id,
                scope_id=claim.scope_id,
                rule_id="R_AGG",
                status=IssueStatus.OPEN,
                rationale="核验目标指标总和/均值及增长率口径是否与声明一致。",
                requested_test=ToolRequest(
                    tool_id="aggregate",
                    parameters={
                        "dataset_id": profile.dataset_id,
                        "target_col_id": target_col,
                        "metric": "sum",
                    },
                ),
            )
        )
    else:
        issue_id = compute_issue_key(claim.claim_id, "missing_field", claim.scope_id, "R_SCHEMA", "")[:12]
        issues.append(
            IssueItem(
                issue_id=f"iss_{issue_id}",
                issue_type="missing_field",
                target_claim_id=claim.claim_id,
                scope_id=claim.scope_id,
                rule_id="R_SCHEMA",
                status=IssueStatus.UNRESOLVABLE,
                rationale="数据集中缺少可核验的数值目标列。",
                requested_test=None,
            )
        )

    return BlindReviewOutput(
        claim_id=claim.claim_id,
        position=ReviewPosition.UNCERTAIN,
        issues=issues,
    )


def _build_mock_challenger_review(claim: ClaimSpec, profile: DatasetProfile) -> BlindReviewOutput:
    target_col, group_col, stratify_cols = _pick_metric_and_group_cols(profile)
    issues: List[IssueItem] = []

    # 1. 混淆分层检验（若存在候选分层列如 is_promo）
    if target_col and group_col and stratify_cols:
        strat_col = stratify_cols[0]
        issue_id = compute_issue_key(claim.claim_id, "confounding", claim.scope_id, "R_STRAT", strat_col)[:12]
        issues.append(
            IssueItem(
                issue_id=f"iss_{issue_id}",
                issue_type="confounding",
                target_claim_id=claim.claim_id,
                scope_id=claim.scope_id,
                rule_id="R_STRAT",
                status=IssueStatus.OPEN,
                rationale="存在候选混淆分组列，需按分层复算组间差以排除辛普森悖论。",
                requested_test=ToolRequest(
                    tool_id="group_compare",
                    parameters={
                        "dataset_id": profile.dataset_id,
                        "target_col_id": target_col,
                        "group_col_id": group_col,
                        "metric": "mean",
                        "stratify_col_id": strat_col,
                    },
                ),
            )
        )

    # 2. 极端值敏感性检验
    if target_col and group_col and claim.claim_type in (
        ClaimType.MAGNITUDE,
        ClaimType.UNIVERSAL,
        ClaimType.CAUSAL,
        ClaimType.DIRECTION,
    ):
        issue_id = compute_issue_key(claim.claim_id, "outlier_sensitivity", claim.scope_id, "R_SENS", target_col)[:12]
        issues.append(
            IssueItem(
                issue_id=f"iss_{issue_id}",
                issue_type="outlier_sensitivity",
                target_claim_id=claim.claim_id,
                scope_id=claim.scope_id,
                rule_id="R_SENS",
                status=IssueStatus.OPEN,
                rationale="需检验 5% 缩尾前后均值差是否发生大幅衰减（排除头部极端值驱动）。",
                requested_test=ToolRequest(
                    tool_id="sensitivity_check",
                    parameters={
                        "dataset_id": profile.dataset_id,
                        "target_col_id": target_col,
                        "group_col_id": group_col,
                        "method": "winsorize_5pct",
                    },
                ),
            )
        )

    # 3. 缺字段 / 因果不可识别缺口检测（上下文提及大促/混淆但表中无分层列）
    context_mentions_confounder = any(
        hint in (claim.context_notes + " " + claim.raw_text).lower()
        for hint in _CONFOUNDER_CONTEXT_HINTS
    )
    if (claim.claim_type == ClaimType.CAUSAL or context_mentions_confounder) and not stratify_cols:
        issue_id = compute_issue_key(claim.claim_id, "missing_field", claim.scope_id, "R_CAUSAL_GAP", "confounder")[
            :12
        ]
        issues.append(
            IssueItem(
                issue_id=f"iss_{issue_id}",
                issue_type="missing_field",
                target_claim_id=claim.claim_id,
                scope_id=claim.scope_id,
                rule_id="R_CAUSAL_GAP",
                status=IssueStatus.UNRESOLVABLE,
                rationale="背景提及潜在混淆因素或因果断言，但当前数据表缺少对应控制/分层字段，无法识别因果净效应。",
                requested_test=None,
            )
        )

    # 4. 口径分母/多列聚合校验（无分组列时检查首列基期总和）
    numeric_cols = [c.col_id for c in profile.columns if c.role_hint == "numeric_metric"]
    if not group_col and len(numeric_cols) >= 2:
        base_col = numeric_cols[0]
        issue_id = compute_issue_key(claim.claim_id, "denominator_check", claim.scope_id, "R_DENOM", base_col)[:12]
        issues.append(
            IssueItem(
                issue_id=f"iss_{issue_id}",
                issue_type="denominator_check",
                target_claim_id=claim.claim_id,
                scope_id=claim.scope_id,
                rule_id="R_DENOM",
                status=IssueStatus.OPEN,
                rationale="核验基期分母列总和，防止增长率分母口径错误。",
                requested_test=ToolRequest(
                    tool_id="aggregate",
                    parameters={
                        "dataset_id": profile.dataset_id,
                        "target_col_id": base_col,
                        "metric": "sum",
                    },
                ),
            )
        )

    return BlindReviewOutput(
        claim_id=claim.claim_id,
        position=ReviewPosition.OPPOSE if issues else ReviewPosition.UNCERTAIN,
        issues=issues,
    )


def run_blind_review(
    claim_spec: ClaimSpec,
    dataset_profile: DatasetProfile,
    *,
    router: Optional[LLMRouter] = None,
) -> Tuple[BlindReviewOutput, BlindReviewOutput, bool]:
    """第 1 轮并行盲审：初审与反方彼此隔离，返回 `(primary_out, challenger_out, model_diversity_lost)`。"""
    active_router = router or LLMRouter(mock_mode=True)
    primary_prompt = load_prompt_template("primary_reviewer")
    challenger_prompt = load_prompt_template("challenger")

    shared_input = (
        f"ClaimSpec: {claim_spec.model_dump_json()}\n"
        f"DatasetProfile: {dataset_profile.model_dump_json()}"
    )

    mock_primary = _build_mock_primary_review(claim_spec, dataset_profile).model_dump(mode="json")
    mock_challenger = _build_mock_challenger_review(claim_spec, dataset_profile).model_dump(mode="json")

    p_resp = active_router.complete(
        role="primary_reviewer",
        messages=[
            {"role": "system", "content": primary_prompt},
            {"role": "user", "content": shared_input},
        ],
        expect_json=True,
        mock_payload=mock_primary if active_router.mock_mode else None,
    )
    c_resp = active_router.complete(
        role="challenger",
        messages=[
            {"role": "system", "content": challenger_prompt},
            {"role": "user", "content": shared_input},
        ],
        expect_json=True,
        mock_payload=mock_challenger if active_router.mock_mode else None,
    )

    primary_out = BlindReviewOutput.model_validate(p_resp.parsed_json or mock_primary)
    challenger_out = BlindReviewOutput.model_validate(c_resp.parsed_json or mock_challenger)
    return primary_out, challenger_out, active_router.model_diversity_lost


def deduplicate_and_plan_tasks(
    reviews: Iterable[Tuple[str, BlindReviewOutput]],
    dataset_hash: str,
    *,
    existing_request_keys: Optional[Set[str]] = None,
    tool_version: str = "1.0",
) -> Tuple[Dict[str, IssueItem], List[VerificationTask], int]:
    """基于 `issue_key` 与 `request_key` 合并去重争议项与白名单验证任务。

    返回 `(deduped_issues, planned_tasks, skipped_duplicates)`。
    越权/非白名单工具请求会被直接忽略，但不会删除对应的争议缺口。
    """
    seen_requests: Set[str] = set(existing_request_keys or set())
    deduped_issues: Dict[str, IssueItem] = {}
    planned_tasks: List[VerificationTask] = []
    skipped_duplicates = 0

    for proposer, review in reviews:
        for issue in review.issues:
            target_key = ""
            if issue.requested_test is not None:
                target_key = compute_request_key(
                    dataset_hash=dataset_hash,
                    tool_id=issue.requested_test.tool_id,
                    tool_version=tool_version,
                    parameters=issue.requested_test.parameters,
                )
            issue_key = compute_issue_key(
                claim_id=issue.target_claim_id,
                issue_type=issue.issue_type,
                scope_id=issue.scope_id,
                rule_id=issue.rule_id,
                target_key=target_key,
            )
            if issue_key not in deduped_issues:
                deduped_issues[issue_key] = issue

            req = issue.requested_test
            if req is None:
                continue
            if req.tool_id not in ALLOWED_MCP_TOOLS:
                # 越权工具直接拒绝入队，但不删争议缺口
                continue

            req_key = target_key
            if req_key in seen_requests:
                skipped_duplicates += 1
                continue

            seen_requests.add(req_key)
            planned_tasks.append(
                VerificationTask(
                    task_id=f"task_{req_key[:10]}",
                    dataset_hash=dataset_hash,
                    tool_id=req.tool_id,
                    tool_version=tool_version,
                    parameters=req.parameters,
                    proposed_by=proposer,
                    rule_id=issue.rule_id,
                    request_key=req_key,
                )
            )

    return deduped_issues, planned_tasks, skipped_duplicates


def evaluate_cross_review_deltas(
    deltas: Iterable[Tuple[str, CrossReviewDelta]],
    *,
    dataset_hash: str,
    executed_request_keys: Set[str],
    existing_evidence_ids: Set[str],
    tool_version: str = "1.0",
) -> Tuple[List[VerificationTask], bool, str]:
    """第 2 轮增量验证规划与防复读守卫。

    仅允许 `ReviewAction` 5 种枚举；拦截重复工具请求、引用不存在证据或无证据妥协。
    返回 `(new_tasks, made_progress, stop_reason)`。
    """
    seen_requests: Set[str] = set(executed_request_keys)
    new_tasks: List[VerificationTask] = []
    grounded_progress = False

    for proposer, delta in deltas:
        if delta.action == ReviewAction.NO_NEW_EVIDENCE:
            continue

        if delta.action == ReviewAction.ADD_NEW_TEST:
            req = delta.requested_test
            if req is None or req.tool_id not in ALLOWED_MCP_TOOLS:
                continue
            req_key = compute_request_key(
                dataset_hash=dataset_hash,
                tool_id=req.tool_id,
                tool_version=tool_version,
                parameters=req.parameters,
            )
            if req_key in seen_requests:
                # 复读已执行过的测试 -> 不入队，不计进展
                continue
            seen_requests.add(req_key)
            new_tasks.append(
                VerificationTask(
                    task_id=f"task_{req_key[:10]}",
                    dataset_hash=dataset_hash,
                    tool_id=req.tool_id,
                    tool_version=tool_version,
                    parameters=req.parameters,
                    proposed_by=proposer,
                    rule_id="R_ROUND2_DELTA",
                    request_key=req_key,
                )
            )
            grounded_progress = True

        elif delta.action in (
            ReviewAction.IDENTIFY_ERROR,
            ReviewAction.REVISE_POSITION,
            ReviewAction.WITHDRAW_OBJECTION,
        ):
            # 必须锚定真实存在的 evidence_id，严禁无证据妥协
            if delta.target_evidence_id and delta.target_evidence_id in existing_evidence_ids:
                grounded_progress = True

    if not grounded_progress:
        return [], False, "NO_PROGRESS"
    return new_tasks, True, ""
