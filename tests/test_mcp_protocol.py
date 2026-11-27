"""Acceptance against the actual stdio subprocess; no provider credentials needed."""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from core.mcp_client import MCPToolError, STAT_TOOLS, StatMCPClient
from core.schema import EvidenceRecord

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures"


def _session(case: str, output: Path) -> StatMCPClient:
    csv_path, = FIXTURES.glob(f"{case}_*.csv")
    return StatMCPClient(
        data_path=csv_path, dataset_id=case, dataset_hash=hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        run_id="protocol", output_root=output,
    )


def test_stdio_discovery_case03_strata_and_ledger(tmp_path):
    async def run():
        async with _session("case_03", tmp_path) as client:
            tools = await client.list_tools()
            assert {item["name"] for item in tools} == STAT_TOOLS
            assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)
            args = {"dataset_id": "case_03", "target_col_id": "col_04", "group_col_id": "col_02"}
            overall = EvidenceRecord.model_validate(await client.call_tool("group_compare", args))
            stratified = EvidenceRecord.model_validate(await client.call_tool("group_compare", {**args, "stratify_col_id": "col_03"}))
            assert overall.result["diff"] == pytest.approx(48)
            assert [row["diff"] for row in stratified.result["strata"]] == pytest.approx([0, 0])
            ledger = await client.call_tool("get_evidence", {"run_id": "protocol"})
            assert [ev["evidence_id"] for ev in ledger] == [overall.evidence_id, stratified.evidence_id]
            assert await client.call_tool("get_evidence", {"evidence_id": overall.evidence_id}) == [overall.model_dump(mode="json")]
            assert [entry["operation"] for entry in client.timings] == ["list_tools", "group_compare", "group_compare", "get_evidence", "get_evidence"]
            assert all(entry["ok"] and entry["elapsed_ms"] > 0 for entry in client.timings)
        persisted = (tmp_path / "protocol/evidence_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        assert [json.loads(row) for row in persisted] == ledger
    asyncio.run(run())


def test_stdio_case04_winsorization(tmp_path):
    async def run():
        async with _session("case_04", tmp_path) as client:
            result = await client.call_tool("sensitivity_check", {
                "dataset_id": "case_04", "target_col_id": "col_03", "group_col_id": "col_02", "method": "winsorize_5pct",
            })
            stats = result["result"]
            assert stats["baseline_diff"] == pytest.approx(48)
            assert stats["adjusted_diff"] == pytest.approx(18, abs=1.5)
            assert stats["relative_change_ratio"] > .50
    asyncio.run(run())


def test_stdio_case06_header_isolation_and_rejected_requests(tmp_path):
    async def run():
        async with _session("case_06", tmp_path) as client:
            inspected = await client.call_tool("inspect_data", {"dataset_id": "case_06"})
            result = inspected["result"]
            assert "ignore previous" not in json.dumps(inspected).lower()
            assert "col_02" in result["column_summaries"]
            assert any("suspicious header" in w for w in result["quality_warnings"])
            counts = await client.call_tool("aggregate", {"dataset_id": "case_06", "target_col_id": "col_04", "metric": "count"})
            assert counts["result"]["value"] == result["row_count"]
            valid = {"dataset_id": "case_06", "target_col_id": "col_04", "metric": "mean"}
            for change in (
                {"target_col_id": "orders"}, {"dataset_id": "../sample.csv"},
                {"metric": "unsupported"}, {"filters": {"col_04": [1]}},
                {"filters": "{}"}, {"path": "sample.csv"}, {"run_id": "../other"},
            ):
                with pytest.raises(MCPToolError):
                    await client.call_tool("aggregate", {**valid, **change})
            with pytest.raises(MCPToolError):
                await client.call_tool("get_evidence", {"run_id": "different_run"})
            assert len(await client.call_tool("get_evidence", {})) == 2
    asyncio.run(run())


def test_stdio_case07_finite_statistics_and_method_rejection(tmp_path):
    async def run():
        async with _session("case_07", tmp_path) as client:
            profile = await client.call_tool("inspect_data", {"dataset_id": "case_07"})
            warnings = profile["result"]["quality_warnings"]
            assert any("NaN" in w for w in warnings) and any("infinite" in w for w in warnings)
            args = {"dataset_id": "case_07", "target_col_id": "col_03", "group_col_id": "col_02"}
            groups = await client.call_tool("group_compare", args)
            assert groups["sample_size"] < profile["result"]["row_count"]
            json.dumps(groups["result"], allow_nan=False)
            with pytest.raises(MCPToolError):
                await client.call_tool("sensitivity_check", {**args, "method": "unsupported"})
            assert len(await client.call_tool("get_evidence", {})) == 2
    asyncio.run(run())
