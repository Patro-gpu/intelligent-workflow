"""One scoped MCP stdio session per audit, using the official Python SDK."""
from __future__ import annotations

from contextlib import AsyncExitStack, contextmanager
from datetime import timedelta
import json
from pathlib import Path
import sys
from time import perf_counter
from typing import Any, Iterator, Sequence

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _REPO_ROOT / "configs/mcp_servers.yaml"
STAT_TOOLS = frozenset({"inspect_data", "aggregate", "group_compare", "sensitivity_check", "get_evidence"})


class MCPToolError(ValueError):
    """The statistical server rejected the request or returned invalid data."""


class StatMCPClient:
    """Use as an async context manager; registration is a trusted startup operation."""

    def __init__(
        self,
        config_path: str | Path = _CONFIG,
        *,
        data_path: str | Path | None = None,
        dataset_id: str | None = None,
        dataset_hash: str | None = None,
        run_id: str = "default_run",
        output_root: str | Path | None = None,
        allowed_roots: Sequence[str | Path] | None = None,
    ) -> None:
        config_path = Path(config_path).absolute()
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))["servers"]["stat_mcp"]
        args = ["-m", "skills.skill_2_stat_mcp.server", "--config", str(config_path), "--run-id", run_id]
        if data_path is not None:
            args.extend(["--data", str(Path(data_path).absolute())])
        if dataset_id is not None:
            args.extend(["--dataset-id", dataset_id])
        if dataset_hash is not None:
            args.extend(["--dataset-hash", dataset_hash])
        if output_root is not None:
            args.extend(["--output-root", str(Path(output_root).absolute())])
        if allowed_roots is not None:
            if not allowed_roots:
                raise ValueError("allowed_roots must not be empty")
            for root in allowed_roots:
                args.extend(["--read-root", str(Path(root).absolute())])

        self._params = StdioServerParameters(
            command=sys.executable, args=args, cwd=_REPO_ROOT,
            env={"PYTHONPATH": str(_REPO_ROOT), "PYTHONUTF8": "1"},
        )
        self._timeout = timedelta(seconds=config["timeout_seconds"])
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.timings: list[dict[str, Any]] = []

    async def __aenter__(self) -> StatMCPClient:
        if self._stack is not None:
            raise RuntimeError("Session is already active")
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(stdio_client(self._params))
            session = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=self._timeout))
            await session.initialize()
            self._stack = stack.pop_all()
            self._session = session
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    @property
    def _connected(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("Open the client with 'async with' first")
        return self._session

    @contextmanager
    def _timed(self, operation: str) -> Iterator[None]:
        start = perf_counter()
        ok = False
        try:
            yield
            ok = True
        finally:
            self.timings.append({"operation": operation, "ok": ok, "elapsed_ms": (perf_counter() - start) * 1000})

    async def list_tools(self) -> list[dict[str, Any]]:
        with self._timed("list_tools"):
            result = await self._connected.list_tools()
            if {t.name for t in result.tools} != STAT_TOOLS or len(result.tools) != len(STAT_TOOLS):
                raise MCPToolError("Unexpected statistical tool manifest")
            return [tool.model_dump(mode="json", exclude_none=True) for tool in result.tools]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict | list[dict]:
        if tool_name not in STAT_TOOLS:
            raise MCPToolError("Unknown statistical tool")
        with self._timed(tool_name):
            result = await self._connected.call_tool(tool_name, arguments)
            if result.isError:
                raise MCPToolError(f"Statistical tool rejected request: {tool_name}")
            # FastMCP v1 serializes a list as one text block per item. This
            # also works with pre-structuredContent versions of the SDK.
            if any(item.type != "text" for item in result.content):
                raise MCPToolError("Malformed statistical response")
            records = [json.loads(item.text) for item in result.content]
            if not all(isinstance(item, dict) for item in records):
                raise MCPToolError("Unexpected statistical response shape")
            if tool_name == "get_evidence":
                return records
            if len(records) != 1:
                raise MCPToolError("Expected a single evidence record")
            return records[0]
