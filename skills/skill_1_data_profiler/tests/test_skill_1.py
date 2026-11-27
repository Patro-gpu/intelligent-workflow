"""Skill 1（数据质量与语义体检）独立单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.security import SecurityError
from skills.skill_1_data_profiler.cli import main as cli_main
from skills.skill_1_data_profiler.skill import inspect_csv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
FIXTURES_DIR = REPO_ROOT / "data/fixtures"


def test_inspect_case_03_identifies_candidate_group_cols_and_quantiles() -> None:
    res = inspect_csv(FIXTURES_DIR / "case_03_promo_confounding.csv", dataset_id="case_03")
    profile = res["profile"]
    summary = res["summary"]

    assert profile["dataset_id"] == "case_03"
    assert profile["row_count"] == 200
    assert profile["col_count"] == 4
    # has_coupon (col_02) 与 is_promo (col_03) 均应被识别为候选分组列
    assert "col_02" in profile["candidate_group_cols"]
    assert "col_03" in profile["candidate_group_cols"]
    assert summary["duplicate_rows"] == 0
    assert summary["column_summaries"]["col_04"]["quantiles"]["p50"] is not None


def test_inspect_case_04_detects_iqr_outliers() -> None:
    res = inspect_csv(FIXTURES_DIR / "case_04_outlier_sensitivity.csv", dataset_id="case_04")
    orders_summary = res["summary"]["column_summaries"]["col_03"]
    assert orders_summary["iqr_outlier_count"] >= 2


def test_inspect_case_06_and_07_security_and_corrupted_warnings() -> None:
    res_06 = inspect_csv(FIXTURES_DIR / "case_06_malicious_headers.csv", dataset_id="case_06")
    warnings_06 = res_06["profile"]["quality_warnings"]
    assert any("suspicious header" in w for w in warnings_06)
    assert any("formula-prefixed" in w for w in warnings_06)

    res_07 = inspect_csv(FIXTURES_DIR / "case_07_corrupted_values.csv", dataset_id="case_07")
    warnings_07 = res_07["profile"]["quality_warnings"]
    assert any("NaN" in w for w in warnings_07)
    assert any("infinite" in w for w in warnings_07)


def test_inspect_blocks_path_traversal() -> None:
    with pytest.raises(SecurityError):
        inspect_csv(FIXTURES_DIR / ".." / ".." / ".env.example")


def test_skill_1_cli_inspect_outputs_valid_json(capsys: object) -> None:
    rc = cli_main(["inspect", "--data", str(FIXTURES_DIR / "case_01_valid_claim.csv")])
    assert rc == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["profile"]["row_count"] == 120
