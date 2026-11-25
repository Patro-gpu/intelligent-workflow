"""阶段 A 仓库结构、配置与环境冒烟测试。"""

from __future__ import annotations

from pathlib import Path
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_required_directories_and_files_exist() -> None:
    required_paths = [
        "AGENTS.md",
        "README.md",
        "pyproject.toml",
        ".env.example",
        ".gitignore",
        "docs/BLUEPRINT_v2.md",
        "configs/models.yaml",
        "configs/mcp_servers.yaml",
        "configs/prompts",
        "scripts/check_secrets.py",
        "core/__init__.py",
        "core/mcp_client.py",
        "skills/skill_1_data_profiler/README.md",
        "skills/skill_2_stat_mcp/README.md",
        "skills/skill_3_claim_planner/README.md",
        "skills/skill_4_evidence_auditor/README.md",
        "workflows/__init__.py",
        "data/__init__.py",
        "data/fixtures",
        "evals/__init__.py",
        "app/__init__.py",
    ]
    for rel in required_paths:
        assert (REPO_ROOT / rel).exists(), f"Missing required path: {rel}"


def test_models_yaml_baseline() -> None:
    cfg = yaml.safe_load((REPO_ROOT / "configs/models.yaml").read_text(encoding="utf-8"))
    assert cfg["roles"]["primary_reviewer"]["provider"] == "deepseek"
    assert cfg["roles"]["challenger"]["provider"] == "doubao"
    assert cfg["roles"]["final_reviewer"]["enabled"] is False
    assert cfg["roles"]["final_reviewer"]["max_calls"] == 1
    assert cfg["workflow"]["max_rounds"] == 2
    assert cfg["robustness"]["warning_change_ratio"] == 0.20
    assert cfg["robustness"]["severe_change_ratio"] == 0.50
    assert cfg["robustness"]["significance_alpha"] == 0.05


def test_mcp_servers_yaml_baseline() -> None:
    cfg = yaml.safe_load((REPO_ROOT / "configs/mcp_servers.yaml").read_text(encoding="utf-8"))
    stat_mcp = cfg["servers"]["stat_mcp"]
    assert stat_mcp["transport"] == "stdio"
    assert "skills.skill_2_stat_mcp.server" in stat_mcp["args"]
    assert "data/fixtures" in stat_mcp["sandbox"]["allowed_read_roots"]
    assert stat_mcp["sandbox"]["output_root"] == "workspace/outputs"
