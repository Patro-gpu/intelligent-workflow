# Skill 2: 白名单受控统计 MCP Server（`skill_2_stat_mcp`）

- **负责人**：成员 2
- **独立 CLI 入口**：`/verify`（阶段 B1 实现）
- **职责**：通过官方 `mcp` / `fastmcp` 启动标准 `stdio` MCP Server，暴露 `inspect_data`, `aggregate`, `group_compare`, `sensitivity_check`, `get_evidence` 五个纯确定性白名单工具，严禁 `eval`/`exec`/`query`。
