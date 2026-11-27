"""阶段 A2 全量核心测试：契约冻结、权限安全层、多模型路由熔断与 7 份基准真值。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from pydantic import ValidationError
import pytest
from scipy.stats.mstats import winsorize

from core.llm_router import BudgetExceededError, LLMRouter
from core.schema import (
    BlindedJudgeInput,
    ClaimSpec,
    ClaimType,
    FinalJudgeOutputAdapter,
    JudgeMappingCorrection,
    JudgeUnresolvedReport,
    SubClaim,
    VerificationTask,
    WorkflowState,
    canonical_json,
    compute_issue_key,
    compute_request_key,
)
from core.security import DatasetRegistry, SecurityError, resolve_safe_path, sanitize_cell_for_export

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "data/fixtures"


# ==============================================================================
# 1. Pydantic v2 严格契约与哈希确定性测试
# ==============================================================================


def test_extra_forbid_blocks_illegal_fields() -> None:
    with pytest.raises(ValidationError):
        ClaimSpec(
            claim_id="c1",
            raw_text="test",
            claim_type=ClaimType.DIRECTION,
            metric="orders",
            undeclared_field="nope",  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        WorkflowState(
            run_id="r1",
            claim_id="c1",
            dataset_hash="abc",
            injected_flag=True,  # type: ignore[call-arg]
        )


def test_final_judge_output_forbids_final_status_and_validates_union() -> None:
    # 严禁终裁模型输出 final_status 覆盖硬规则
    with pytest.raises(ValidationError):
        FinalJudgeOutputAdapter.validate_python(
            {
                "action": "propose_mapping_correction",
                "claim_id": "c1",
                "issue_id": "i1",
                "candidate_id": "cand_a",
                "rule_id": "rule_1",
                "evidence_ids": ["ev_1"],
                "reason_code": "mapping_fix",
                "proposed_mapping": {"ev_1": "supports"},
                "final_status": "verified",
            }
        )

    with pytest.raises(ValidationError):
        FinalJudgeOutputAdapter.validate_python(
            {
                "action": "report_unresolved",
                "claim_id": "c1",
                "issue_id": "i1",
                "reason_code": "ambiguous_unit",
                "final_status": "insufficient",
            }
        )

    valid_correction = FinalJudgeOutputAdapter.validate_python(
        {
            "action": "propose_mapping_correction",
            "claim_id": "c1",
            "issue_id": "i1",
            "candidate_id": "cand_a",
            "rule_id": "rule_1",
            "evidence_ids": ["ev_1"],
            "reason_code": "mapping_fix",
            "proposed_mapping": {"ev_1": "supports"},
        }
    )
    assert isinstance(valid_correction, JudgeMappingCorrection)

    valid_unresolved = FinalJudgeOutputAdapter.validate_python(
        {
            "action": "report_unresolved",
            "claim_id": "c1",
            "issue_id": "i1",
            "reason_code": "ambiguous_unit",
            "unresolved_issue_ids": ["i1"],
        }
    )
    assert isinstance(valid_unresolved, JudgeUnresolvedReport)


def test_blinded_judge_input_forbids_model_name_and_confidence() -> None:
    claim = ClaimSpec(
        claim_id="c1",
        raw_text="orders increased",
        claim_type=ClaimType.DIRECTION,
        metric="orders",
    )
    with pytest.raises(ValidationError):
        BlindedJudgeInput(
            claim_id="c1",
            rule_version="1.0",
            unresolved_issue_id="i1",
            issue_type="evidence_mapping",
            claim_spec=claim,
            candidate_interpretations=[{"id": "A"}],
            admissible_evidence=[],
            model_name="deepseek",  # type: ignore[call-arg]
        )


def test_claim_spec_limits_sub_claims_to_two() -> None:
    sub = SubClaim(sub_claim_id="s", claim_type=ClaimType.DIRECTION, metric="m")
    # 2 sub-claims allowed
    ok = ClaimSpec(
        claim_id="c1",
        raw_text="test",
        claim_type=ClaimType.CAUSAL,
        metric="m",
        sub_claims=[sub, sub.model_copy(update={"sub_claim_id": "s2"})],
    )
    assert len(ok.sub_claims) == 2

    # 3 sub-claims rejected
    with pytest.raises(ValidationError):
        ClaimSpec(
            claim_id="c1",
            raw_text="test",
            claim_type=ClaimType.CAUSAL,
            metric="m",
            sub_claims=[
                sub,
                sub.model_copy(update={"sub_claim_id": "s2"}),
                sub.model_copy(update={"sub_claim_id": "s3"}),
            ],
        )


def test_canonical_json_and_deterministic_keys() -> None:
    j1 = canonical_json({"b": 2, "a": {"y": 1, "x": 0}})
    j2 = canonical_json({"a": {"x": 0, "y": 1}, "b": 2})
    assert j1 == j2

    k1 = compute_request_key("h1", "group_compare", "1.0", {"b": 2, "a": 1})
    k2 = compute_request_key("h1", "group_compare", "1.0", {"a": 1, "b": 2})
    assert k1 == k2

    task = VerificationTask(
        task_id="t1",
        dataset_hash="h1",
        tool_id="group_compare",
        tool_version="1.0",
        parameters={"a": 1, "b": 2},
    )
    assert task.request_key == k1

    ik1 = compute_issue_key("c1", "confounding", "global", "r1", "col_03")
    ik2 = compute_issue_key("c1", "confounding", "global", "r1", "col_03")
    assert ik1 == ik2


# ==============================================================================
# 2. 安全权限层测试（路径穿越、符号链接、公式注入、恶意表头与 NaN/Inf）
# ==============================================================================


def test_resolve_safe_path_blocks_traversal_and_symlink_escape(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()

    safe_file = allowed / "ok.csv"
    safe_file.write_text("a,b\n1,2\n", encoding="utf-8")
    secret_file = outside / "secret.env"
    secret_file.write_text("SECRET=1\n", encoding="utf-8")

    # 合法路径放行
    assert resolve_safe_path(safe_file, [allowed]) == safe_file.resolve()

    # ../ 路径穿越拦截
    with pytest.raises(SecurityError):
        resolve_safe_path(allowed / ".." / "outside" / "secret.env", [allowed])

    # 符号链接逃逸拦截
    escape_link = allowed / "evil_link.csv"
    try:
        escape_link.symlink_to(secret_file)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        # Windows without SeCreateSymbolicLinkPrivilege still supports directory junctions.
        import _winapi
        junction = allowed / "linked_dir"
        _winapi.CreateJunction(str(outside), str(junction))
        escape_link = junction / secret_file.name
    with pytest.raises(SecurityError):
        resolve_safe_path(escape_link, [allowed])


def test_sanitize_cell_for_export_escapes_formula_injection() -> None:
    assert sanitize_cell_for_export("=CMD|' /C calc'!A0") == "'=CMD|' /C calc'!A0"
    assert sanitize_cell_for_export("+SUM(A1:A5)") == "'+SUM(A1:A5)"
    assert sanitize_cell_for_export("-1+1") == "'-1+1"
    assert sanitize_cell_for_export("@SUM(1,2)") == "'@SUM(1,2)"
    assert sanitize_cell_for_export("\t=1+1") == "'\t=1+1"
    assert sanitize_cell_for_export("normal_text") == "normal_text"
    assert sanitize_cell_for_export(123.45) == 123.45


def test_csv_registration_limits_and_immutable_dataset_identity(tmp_path: Path) -> None:
    path = tmp_path / "input.csv"
    registry = DatasetRegistry([tmp_path], max_rows=1, max_cols=2)
    path.write_text("count,n\n1,2\n", encoding="utf-8")
    original = registry.register_csv(path, "registered")
    path.write_text("count,n\n1,3\n", encoding="utf-8")
    with pytest.raises(SecurityError):
        registry.register_csv(path, "registered")
    assert registry.get_profile("registered").dataset_hash == original.dataset_hash
    for oversized in ("a,b,c\n1,2,3\n", "a,b\n1,2\n3,4\n"):
        path.write_text(oversized, encoding="utf-8")
        with pytest.raises(SecurityError):
            registry.register_csv(path)


def test_dataset_registry_isolates_case_06_malicious_headers_and_detects_case_07_corrupted() -> None:
    registry = DatasetRegistry(allowed_roots=[FIXTURES_DIR])

    # Case 06: 恶意表头隔离为 col_01..col_04
    profile_06 = registry.register_csv(FIXTURES_DIR / "case_06_malicious_headers.csv", dataset_id="case_06")
    df_06 = registry.get_dataframe("case_06")
    assert list(df_06.columns) == ["col_01", "col_02", "col_03", "col_04"]
    assert all(col.col_id.startswith("col_") for col in profile_06.columns)
    assert any("suspicious header" in w for w in profile_06.quality_warnings)
    assert profile_06.columns[1].sanitized_name == "col_02"
    assert any("formula-prefixed" in w for w in profile_06.quality_warnings)
    # 验证恶意单元格可安全转义
    raw_evil_cell = str(df_06.loc[0, "col_02"])
    assert raw_evil_cell.startswith("=CMD")
    assert sanitize_cell_for_export(raw_evil_cell).startswith("'=CMD")

    # Case 07: 检测 NaN 与 Inf / -Inf
    profile_07 = registry.register_csv(FIXTURES_DIR / "case_07_corrupted_values.csv", dataset_id="case_07")
    assert any("NaN" in w for w in profile_07.quality_warnings)
    assert any("infinite" in w for w in profile_07.quality_warnings)

    # 越权路径在注册阶段被硬拦截
    with pytest.raises(SecurityError):
        registry.register_csv(FIXTURES_DIR / ".." / ".." / ".env.example")


# ==============================================================================
# 3. 7 份确定性基准集真值与 manifest.json 精确验证
# ==============================================================================


def test_benchmark_manifest_and_all_7_fixtures_truth() -> None:
    manifest_path = FIXTURES_DIR / "manifest.json"
    assert manifest_path.exists(), "manifest.json must be generated in data/fixtures/"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["case_count"] == 7
    assert len(manifest["cases"]) == 7

    for case in manifest["cases"]:
        csv_path = FIXTURES_DIR / case["filename"]
        assert csv_path.exists(), f"Missing fixture CSV: {csv_path}"
        # 校验每条 claim_spec 严格符合冻结的 ClaimSpec 契约
        ClaimSpec.model_validate(case["claim_spec"])

    # Case 01: 正常无陷阱
    df_01 = pd.read_csv(FIXTURES_DIR / "case_01_valid_claim.csv")
    assert len(df_01) == 120
    m1_t = df_01.loc[df_01["has_coupon"] == 1, "orders"].mean()
    m1_c = df_01.loc[df_01["has_coupon"] == 0, "orders"].mean()
    assert m1_t == pytest.approx(130.0, abs=1e-6)
    assert m1_c == pytest.approx(100.0, abs=1e-6)
    assert (m1_t - m1_c) == pytest.approx(30.0, abs=1e-6)

    # Case 02: 分母口径错误
    df_02 = pd.read_csv(FIXTURES_DIR / "case_02_denominator_error.csv")
    base_sum = df_02["base_revenue_wan"].sum()
    curr_sum = df_02["current_revenue_wan"].sum()
    assert base_sum == pytest.approx(100.0, abs=1e-6)
    assert curr_sum == pytest.approx(125.0, abs=1e-6)
    assert ((curr_sum - base_sum) / base_sum) == pytest.approx(0.25, abs=1e-6)

    # Case 03: 大促混淆 / 辛普森悖论核心演示案例（总均值 154 vs 106，差 48，分层差严格为 0）
    df_03 = pd.read_csv(FIXTURES_DIR / "case_03_promo_confounding.csv")
    t_mean = df_03.loc[df_03["has_coupon"] == 1, "orders"].mean()
    c_mean = df_03.loc[df_03["has_coupon"] == 0, "orders"].mean()
    assert t_mean == pytest.approx(154.0, abs=1e-6)
    assert c_mean == pytest.approx(106.0, abs=1e-6)
    assert (t_mean - c_mean) == pytest.approx(48.0, abs=1e-6)

    for promo_val in (0, 1):
        sub = df_03[df_03["is_promo"] == promo_val]
        sub_diff = (
            sub.loc[sub["has_coupon"] == 1, "orders"].mean()
            - sub.loc[sub["has_coupon"] == 0, "orders"].mean()
        )
        assert sub_diff == pytest.approx(0.0, abs=1e-6)

    # Case 04: 极端异常值敏感（原始差 48，5% 缩尾后衰减超 50%）
    df_04 = pd.read_csv(FIXTURES_DIR / "case_04_outlier_sensitivity.csv")
    raw_t = df_04.loc[df_04["has_coupon"] == 1, "orders"].to_numpy(dtype=float)
    raw_c = df_04.loc[df_04["has_coupon"] == 0, "orders"].to_numpy(dtype=float)
    raw_diff = float(raw_t.mean() - raw_c.mean())
    assert raw_diff == pytest.approx(48.0, abs=1e-6)

    win_t = np.asarray(winsorize(raw_t, limits=(0.05, 0.05)), dtype=float)
    win_c = np.asarray(winsorize(raw_c, limits=(0.05, 0.05)), dtype=float)
    win_diff = float(win_t.mean() - win_c.mean())
    relative_change = abs(win_diff - raw_diff) / abs(raw_diff)
    assert win_diff == pytest.approx(18.0, abs=1.5)
    assert relative_change > 0.50

    # Case 05: 缺字段不可识别
    df_05 = pd.read_csv(FIXTURES_DIR / "case_05_missing_confounder.csv")
    assert "is_promo" not in df_05.columns


# ==============================================================================
# 4. LLMRouter 离线 Mock、预算熔断、Qwen 降级与脱敏日志测试
# ==============================================================================


def test_llm_router_mock_mode_and_sanitized_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_secret = "sk-mock-dummy-key-1234567890"
    monkeypatch.setenv("DEEPSEEK_API_KEY", fake_secret)
    router = LLMRouter(mock_mode=True, max_budget_cny=1.0, log_dir=tmp_path)

    resp = router.complete(
        role="primary_reviewer",
        messages=[{"role": "user", "content": "Inspect claim"}],
        expect_json=True,
    )
    assert resp.mock_mode is True
    assert resp.used_provider == "deepseek"
    assert resp.used_fallback is False
    assert isinstance(resp.parsed_json, dict)
    assert router.spent_cny > 0

    log_files = list(tmp_path.glob("audit_*.jsonl"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    assert fake_secret not in log_text
    assert '"used_provider": "deepseek"' in log_text


def test_llm_router_budget_circuit_breaker(tmp_path: Path) -> None:
    router = LLMRouter(mock_mode=True, max_budget_cny=0.0001, log_dir=tmp_path)
    with pytest.raises(BudgetExceededError):
        router.complete(
            role="primary_reviewer",
            messages=[{"role": "user", "content": "Trigger budget breaker"}],
            max_tokens=512,
        )
    assert router.call_count == 0
    assert router.spent_cny == 0.0


def test_llm_router_fallback_to_qwen_and_diversity_lost(tmp_path: Path) -> None:
    router = LLMRouter(
        mock_mode=True,
        max_budget_cny=1.0,
        log_dir=tmp_path,
        simulate_primary_failure_roles={"primary_reviewer", "challenger"},
    )
    r1 = router.complete(
        role="primary_reviewer",
        messages=[{"role": "user", "content": "Review 1"}],
    )
    assert r1.used_fallback is True
    assert r1.used_provider == "qwen"
    assert router.model_diversity_lost is False

    r2 = router.complete(
        role="challenger",
        messages=[{"role": "user", "content": "Review 2"}],
    )
    assert r2.used_fallback is True
    assert r2.used_provider == "qwen"
    assert router.model_diversity_lost is True
