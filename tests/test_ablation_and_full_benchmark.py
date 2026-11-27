"""阶段 B4 验收测试：20 份完整分层基准集、R0/R1/R2/R5 消融评测与观测台代码约束。"""

from __future__ import annotations

import ast
from collections import Counter
import json
from pathlib import Path

from data.generate_benchmark import generate_full_fixtures
from evals.run_ablation import run_ablation

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_full_benchmark_generates_20_stratified_cases(tmp_path: Path) -> None:
    manifest = generate_full_fixtures(tmp_path, seed=42)
    assert manifest["case_count"] == 20
    assert len(manifest["cases"]) == 20

    counts = Counter(c["category"] for c in manifest["cases"])
    assert counts["valid_claim"] == 3
    assert counts["denominator_error"] == 3
    assert counts["promo_confounding"] == 4
    assert counts["outlier_sensitivity"] + counts["subgroup_reversal"] == 4
    assert counts["missing_confounder"] == 4
    assert counts["security_redteam_injection"] + counts["security_redteam_corrupted_values"] == 2

    manifest_file = tmp_path / "manifest.json"
    assert manifest_file.exists()
    loaded = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert loaded["case_count"] == 20


def test_ablation_r5_outperforms_r0_r1_r2_on_20_cases(tmp_path: Path) -> None:
    res = run_ablation(scale="full", enable_final_judge=False, output_dir=tmp_path)
    summary = res["summary"]

    r0 = summary["R0_fixed_script"]
    r1 = summary["R1_single_llm_bare"]
    r2 = summary["R2_single_agent_mcp"]
    r5 = summary["R5_shuzheng_audit"]

    assert r5["total_cases"] == 20
    assert r5["invariants_pass_rate"] == 1.0
    assert r5["accuracy"] >= r2["accuracy"]
    assert r5["accuracy"] > r0["accuracy"]
    assert r5["accuracy"] > r1["accuracy"]
    assert r5["false_verified_rate"] <= r2["false_verified_rate"]
    assert (tmp_path / "ablation_report.md").exists()
    assert (tmp_path / "ablation_results.csv").exists()


def test_streamlit_dashboard_syntax_and_line_budget() -> None:
    dash_path = REPO_ROOT / "app/dashboard.py"
    text = dash_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) <= 250, f"app/dashboard.py has {len(lines)} lines, exceeds 250 line budget"
    ast.parse(text)
