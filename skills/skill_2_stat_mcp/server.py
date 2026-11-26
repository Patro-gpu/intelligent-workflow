"""Skill 2: 官方 FastMCP 标准 stdio MCP Server 入口（`skills.skill_2_stat_mcp.server`）。

在 Python 3.11+ 环境安装 `mcp>=1.0` 后，通过标准 stdio 协议暴露 5 个受控白名单统计工具：
- `inspect_data`
- `aggregate`
- `group_compare`
- `sensitivity_check`
- `get_evidence`
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from skills.skill_2_stat_mcp.engine import StatAuditEngine

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - requires Python >=3.10 + mcp package
    FastMCP = None  # type: ignore[assignment]
    _MCP_IMPORT_ERROR = exc
else:
    _MCP_IMPORT_ERROR = None


def build_mcp_server(engine: Optional[StatAuditEngine] = None) -> Any:
    if FastMCP is None:
        raise RuntimeError(
            "Official 'mcp' package is required to run the stdio MCP server (Python >=3.10, `pip install -e .`)."
        ) from _MCP_IMPORT_ERROR

    active_engine = engine or StatAuditEngine()
    mcp = FastMCP("shuzheng-stat-mcp")

    @mcp.tool()
    def inspect_data(dataset_id: str, run_id: str = "default_run", claim_id: str = "claim_01") -> Dict[str, Any]:
        """Inspect a registered dataset by dataset_id using safe col_id mappings."""
        return active_engine.inspect_data(dataset_id, run_id=run_id, claim_id=claim_id).model_dump(mode="json")

    @mcp.tool()
    def aggregate(
        dataset_id: str,
        target_col_id: str,
        metric: str,
        filters: Optional[Dict[str, Any]] = None,
        run_id: str = "default_run",
        claim_id: str = "claim_01",
    ) -> Dict[str, Any]:
        """Run a whitelisted deterministic aggregation (mean|median|sum|count|rate)."""
        return active_engine.aggregate(
            dataset_id,
            target_col_id,
            metric,
            filters=filters,
            run_id=run_id,
            claim_id=claim_id,
        ).model_dump(mode="json")

    @mcp.tool()
    def group_compare(
        dataset_id: str,
        target_col_id: str,
        group_col_id: str,
        metric: str = "mean",
        stratify_col_id: Optional[str] = None,
        run_id: str = "default_run",
        claim_id: str = "claim_01",
    ) -> Dict[str, Any]:
        """Compare groups with optional stratification by stratify_col_id."""
        return active_engine.group_compare(
            dataset_id,
            target_col_id,
            group_col_id,
            metric=metric,
            stratify_col_id=stratify_col_id,
            run_id=run_id,
            claim_id=claim_id,
        ).model_dump(mode="json")

    @mcp.tool()
    def sensitivity_check(
        dataset_id: str,
        target_col_id: str,
        group_col_id: str,
        method: str = "winsorize_5pct",
        run_id: str = "default_run",
        claim_id: str = "claim_01",
    ) -> Dict[str, Any]:
        """Run whitelisted outlier sensitivity check (winsorize_5pct|trim_top_1pct|median_vs_mean|drop_outliers_iqr)."""
        return active_engine.sensitivity_check(
            dataset_id,
            target_col_id,
            group_col_id,
            method=method,
            run_id=run_id,
            claim_id=claim_id,
        ).model_dump(mode="json")

    @mcp.tool()
    def get_evidence(run_id: str, evidence_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch persisted deterministic evidence records for run_id."""
        return active_engine.get_evidence(run_id, evidence_id=evidence_id)

    return mcp


def main() -> None:  # pragma: no cover
    server = build_mcp_server()
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
