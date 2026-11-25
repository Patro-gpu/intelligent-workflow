"""《数证 Audit》全局冻结 Pydantic v2 数据契约与枚举（V2）。

所有数据模型均强制启用 `ConfigDict(extra="forbid")`，
确保跨 Agent、MCP 工具与确定性规则引擎之间的字段边界不可越权。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class StrictModel(BaseModel):
    """全局严格契约基类：禁止任意未声明字段。"""

    model_config = ConfigDict(extra="forbid")


# ==============================================================================
# 1. 全局枚举
# ==============================================================================


class ClaimType(str, Enum):
    DESCRIPTIVE = "descriptive"
    DIRECTION = "direction"
    MAGNITUDE = "magnitude"
    UNIVERSAL = "universal"
    CAUSAL = "causal"


class VerdictStatus(str, Enum):
    VERIFIED = "verified"
    LIMITED = "limited"
    CONTRADICTED = "contradicted"
    INSUFFICIENT = "insufficient"


class ReviewPosition(str, Enum):
    SUPPORT = "support"
    OPPOSE = "oppose"
    UNCERTAIN = "uncertain"


class ReviewAction(str, Enum):
    ADD_NEW_TEST = "add_new_test"
    IDENTIFY_ERROR = "identify_error"
    REVISE_POSITION = "revise_position"
    WITHDRAW_OBJECTION = "withdraw_objection"
    NO_NEW_EVIDENCE = "no_new_evidence"


class IssueStatus(str, Enum):
    OPEN = "open"
    TEST_PENDING = "test_pending"
    RESOLVED = "resolved"
    UNRESOLVABLE = "unresolvable"
    WITHDRAWN = "withdrawn"


# ==============================================================================
# 2. 规范化序列化与哈希工具
# ==============================================================================


def _normalize_for_json(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return _normalize_for_json(obj.model_dump(mode="json"))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _normalize_for_json(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_normalize_for_json(v) for v in obj]
    if isinstance(obj, set):
        normalized = [_normalize_for_json(v) for v in obj]
        return sorted(normalized, key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
    return obj


def canonical_json(obj: Any) -> str:
    """将对象规范化为键排序、紧凑、无歧义的 JSON 字符串。"""
    normalized = _normalize_for_json(obj)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_request_key(
    dataset_hash: str,
    tool_id: str,
    tool_version: str,
    parameters: Dict[str, Any],
) -> str:
    """计算工具调用的确定性 SHA-256 去重键。"""
    payload = {
        "dataset_hash": dataset_hash,
        "tool_id": tool_id,
        "tool_version": tool_version,
        "parameters": parameters,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def compute_issue_key(
    claim_id: str,
    issue_type: str,
    scope_id: str,
    rule_id: str,
    target_key: str = "",
) -> str:
    """计算审计争议项的确定性 SHA-256 去重键。"""
    payload = {
        "claim_id": claim_id,
        "issue_type": issue_type,
        "scope_id": scope_id,
        "rule_id": rule_id,
        "target_key": target_key,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ==============================================================================
# 3. 数据集与命题契约
# ==============================================================================


class ColumnMapping(StrictModel):
    col_id: str
    sanitized_name: str
    raw_name_hash: str
    dtype: str
    role_hint: str = "unknown"


class DatasetProfile(StrictModel):
    dataset_id: str
    dataset_hash: str
    row_count: int = Field(ge=0)
    col_count: int = Field(ge=0)
    columns: List[ColumnMapping]
    quality_warnings: List[str] = Field(default_factory=list)
    candidate_group_cols: List[str] = Field(default_factory=list)


class SubClaim(StrictModel):
    sub_claim_id: str
    claim_type: ClaimType
    metric: str
    direction: Optional[str] = None
    target_value: Optional[float] = None
    unit: Optional[str] = None
    scope_id: str = "global"
    required: bool = True


class ClaimSpec(StrictModel):
    claim_id: str
    raw_text: str
    claim_type: ClaimType
    metric: str
    direction: Optional[str] = None
    target_value: Optional[float] = None
    unit: Optional[str] = None
    scope_id: str = "global"
    time_window: Optional[str] = None
    context_notes: str = ""
    sub_claims: List[SubClaim] = Field(default_factory=list)

    @field_validator("sub_claims")
    @classmethod
    def validate_sub_claims_limit(cls, value: List[SubClaim]) -> List[SubClaim]:
        if len(value) > 2:
            raise ValueError("ClaimSpec.sub_claims must contain at most 2 sub-claims")
        return value


# ==============================================================================
# 4. 辩论与验证任务契约
# ==============================================================================


class ToolRequest(StrictModel):
    tool_id: str
    parameters: Dict[str, Any] = Field(default_factory=dict)


class IssueItem(StrictModel):
    issue_id: str
    issue_type: str
    target_claim_id: str
    scope_id: str = "global"
    rule_id: str = ""
    status: IssueStatus = IssueStatus.OPEN
    rationale: str = ""
    requested_test: Optional[ToolRequest] = None
    evidence_ids: List[str] = Field(default_factory=list)


class BlindReviewOutput(StrictModel):
    claim_id: str
    position: ReviewPosition
    issues: List[IssueItem] = Field(default_factory=list)


class CrossReviewDelta(StrictModel):
    claim_id: str
    action: ReviewAction
    issue_id: Optional[str] = None
    target_evidence_id: Optional[str] = None
    error_type: Optional[str] = None
    requested_test: Optional[ToolRequest] = None
    rationale: str = ""


class VerificationTask(StrictModel):
    task_id: str
    dataset_hash: str
    tool_id: str
    tool_version: str = "1.0"
    parameters: Dict[str, Any] = Field(default_factory=dict)
    proposed_by: str = ""
    rule_id: str = ""
    request_key: str = ""

    @model_validator(mode="after")
    def populate_request_key(self) -> "VerificationTask":
        if not self.request_key:
            self.request_key = compute_request_key(
                dataset_hash=self.dataset_hash,
                tool_id=self.tool_id,
                tool_version=self.tool_version,
                parameters=self.parameters,
            )
        return self


class EvidenceRecord(StrictModel):
    evidence_id: str
    claim_id: str
    dataset_hash: str
    tool_id: str
    tool_version: str = "1.0"
    execution_status: Literal["ok", "failed", "rejected"] = "ok"
    estimand: str = ""
    scope_id: str = "global"
    sample_size: Optional[int] = Field(default=None, ge=0)
    unit: Optional[str] = None
    result: Dict[str, Any] = Field(default_factory=dict)
    applicable: bool = True
    assumption_checks: Dict[str, Any] = Field(default_factory=dict)
    limitations: List[str] = Field(default_factory=list)
    request_hash: str = ""
    result_hash: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ==============================================================================
# 5. 盲化终裁与裁决输出契约
# ==============================================================================


class BlindedJudgeInput(StrictModel):
    """盲化终裁输入：严禁包含模型名、发言顺序、置信度、自由辩论长文。"""

    claim_id: str
    rule_version: str
    unresolved_issue_id: str
    issue_type: str
    claim_spec: ClaimSpec
    candidate_interpretations: List[Dict[str, Any]]
    admissible_evidence: List[EvidenceRecord]
    missing_checks: List[str] = Field(default_factory=list)
    allowed_actions: List[Literal["propose_mapping_correction", "report_unresolved"]] = Field(
        default_factory=lambda: ["propose_mapping_correction", "report_unresolved"]
    )


class JudgeMappingCorrection(StrictModel):
    """终裁模型仅允许提出可机械核验的证据映射修正，禁止输出 final_status。"""

    action: Literal["propose_mapping_correction"] = "propose_mapping_correction"
    claim_id: str
    issue_id: str
    candidate_id: str
    rule_id: str
    evidence_ids: List[str]
    reason_code: str
    proposed_mapping: Dict[str, Any]
    unresolved_issue_ids: List[str] = Field(default_factory=list)


class JudgeUnresolvedReport(StrictModel):
    """终裁模型上报无法机械核验的未决冲突，禁止输出 final_status。"""

    action: Literal["report_unresolved"] = "report_unresolved"
    claim_id: str
    issue_id: str
    reason_code: str
    unresolved_issue_ids: List[str] = Field(default_factory=list)


FinalJudgeOutput = Annotated[
    Union[JudgeMappingCorrection, JudgeUnresolvedReport],
    Field(discriminator="action"),
]
FinalJudgeOutputAdapter: TypeAdapter[Any] = TypeAdapter(FinalJudgeOutput)


class AuditVerdict(StrictModel):
    claim_id: str
    status: VerdictStatus
    cited_evidence_ids: List[str] = Field(default_factory=list)
    key_findings: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    revised_claim: Optional[str] = None
    stop_reason: str = ""
    invariants_passed: bool = True


# ==============================================================================
# 6. 全局有界状态机契约
# ==============================================================================


class WorkflowState(StrictModel):
    run_id: str
    round: int = Field(default=1, ge=1, le=2)
    claim_id: str
    dataset_hash: str
    rule_version: str = "1.0"
    evidence: Dict[str, EvidenceRecord] = Field(default_factory=dict)
    issues: Dict[str, IssueItem] = Field(default_factory=dict)
    executed_tests: Dict[str, str] = Field(default_factory=dict)
    pending_tests: List[VerificationTask] = Field(default_factory=list)
    required_checks_complete: bool = False
    core_supported: bool = False
    full_claim_supported: bool = False
    decisive_refutation: bool = False
    material_limitation: bool = False
    unresolved_conflict: bool = False
    critical_gap: bool = False
    new_information: bool = False
    conflict_type: Optional[str] = None
    conflict_is_material: bool = False
    cheap_resolution_exhausted: bool = False
    verification_path_exists: bool = False
    review_closed: bool = False
    judge_calls: int = Field(default=0, ge=0, le=1)
    judge_closed: bool = False
    model_diversity_lost: bool = False
    remaining_budget: float = Field(default=1.0, ge=0.0)
    reserved_judge_cost: float = Field(default=0.0, ge=0.0)
    remaining_tool_calls: int = Field(default=12, ge=0)
    duplicate_tool_executions: int = Field(default=0, ge=0)
    invalid_evidence_accepted: int = Field(default=0, ge=0)
    judge_rule_overrides: int = Field(default=0, ge=0)
    tool_calls_after_terminal: int = Field(default=0, ge=0)
    status: Optional[VerdictStatus] = None
    stop_reason: str = ""
