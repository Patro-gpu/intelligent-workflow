"""Skill 2: 官方 FastMCP 标准 stdio MCP Server 入口（`skills.skill_2_stat_mcp.server`）。

在 Python 3.11+ 环境安装 `mcp>=1.0` 后，通过标准 stdio 协议暴露 5 个受控白名单统计工具：
- `inspect_data`
- `aggregate`
- `group_compare`
- `sensitivity_check`
- `get_evidence`
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from jsonschema import Draft202012Validator
from mcp.server.fastmcp import FastMCP

from core.security import DatasetRegistry, SecurityError, validate_run_id
from skills.skill_1_data_profiler.skill import DEFAULT_MCP_CONFIG, REPO_ROOT, load_sandbox_config
from skills.skill_2_stat_mcp.engine import StatAuditEngine


class ScopedStatServer(FastMCP):
    """Validate the raw MCP object before FastMCP drops or coerces arguments."""

    def __init__(self, run_id: str):
        super().__init__("shuzheng-stat-mcp")
        self.run_id = validate_run_id(run_id)

    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.inputSchema["additionalProperties"] = False
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        tools = await self.list_tools()
        schema = next((tool.inputSchema for tool in tools if tool.name == name), None)
        if schema is None or not Draft202012Validator(schema).is_valid(arguments):
            raise ValueError("Invalid statistical tool arguments")
        if arguments.get("run_id", self.run_id) != self.run_id:
            raise ValueError("Request is outside the active run")
        return await super().call_tool(name, {**arguments, "run_id": self.run_id})


def build_mcp_server(engine: Optional[StatAuditEngine] = None, *, run_id: str = "default_run") -> FastMCP:
    active_engine = engine or StatAuditEngine()
    mcp = ScopedStatServer(run_id)

    @mcp.tool()
    def inspect_data(dataset_id: str, run_id: str = "default_run", claim_id: str = "claim_01") -> Dict[str, Any]:
        """Inspect a registered dataset by dataset_id using safe col_id mappings."""
        return active_engine.inspect_data(dataset_id, run_id=run_id, claim_id=claim_id).model_dump(mode="json")

    @mcp.tool()
    def aggregate(
        dataset_id: str,
        target_col_id: str,
        metric: Literal["mean", "median", "sum", "count", "rate"],
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
        metric: Literal["mean", "median"] = "mean",
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
        method: Literal["winsorize_5pct", "trim_top_1pct", "median_vs_mean", "drop_outliers_iqr"] = "winsorize_5pct",
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
    def get_evidence(run_id: str = "default_run", evidence_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch persisted deterministic evidence records for run_id."""
        return active_engine.get_evidence(run_id, evidence_id=evidence_id)

    return mcp


def main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Run the scoped statistical stdio server")
    parser.add_argument("--config", type=Path, default=DEFAULT_MCP_CONFIG)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--dataset-id")
    parser.add_argument("--dataset-hash")
    parser.add_argument("--run-id", default="default_run")
    parser.add_argument("--read-root", type=Path, action="append")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    sandbox = load_sandbox_config(args.config)
    registry = DatasetRegistry(
        allowed_roots=args.read_root or [REPO_ROOT / root for root in sandbox["allowed_read_roots"]],
        max_file_size_mb=sandbox["max_file_size_mb"],
        max_rows=sandbox["max_rows"],
        max_cols=sandbox["max_cols"],
    )
    engine = StatAuditEngine(registry=registry, output_root=args.output_root or REPO_ROOT / sandbox["output_root"])
    if args.data is not None:
        profile = engine.register_dataset(args.data, dataset_id=args.dataset_id)
        if args.dataset_hash is not None and profile["dataset_hash"] != args.dataset_hash:
            raise SecurityError("Dataset changed since audit initialization")
    server = build_mcp_server(engine, run_id=args.run_id)
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
