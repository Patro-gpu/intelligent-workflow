# Skill 2: 白名单受控统计 MCP Server（`skill_2_stat_mcp`）

- **负责人**：成员 2
- **独立 CLI 入口**：`verify`（对应 `/verify`）
- **MCP 启动入口**：`python -m skills.skill_2_stat_mcp.server`（标准 `stdio` 协议，需 Python 3.11+ 与 `mcp>=1.0`）
- **5 个白名单确定性工具**（严禁 `eval`/`exec`/`query`）：
  1. `inspect_data(dataset_id)`
  2. `aggregate(dataset_id, target_col_id, metric, filters=None)`
  3. `group_compare(dataset_id, target_col_id, group_col_id, metric, stratify_col_id=None)`
  4. `sensitivity_check(dataset_id, target_col_id, group_col_id, method)`
  5. `get_evidence(run_id, evidence_id=None)`

## 独立运行示例（`/verify`）

```bash
# 验证 Case 03 辛普森悖论分层复算（原始差 48 -> 分层差 0）
python -m skills.skill_2_stat_mcp.cli verify \
  --data data/fixtures/case_03_promo_confounding.csv \
  --tool group_compare --target-col col_04 --group-col col_02 --stratify-col col_03

# 验证 Case 04 5% 缩尾敏感性（相对变化率 > 50%）
python -m skills.skill_2_stat_mcp.cli verify \
  --data data/fixtures/case_04_outlier_sensitivity.csv \
  --tool sensitivity_check --target-col col_03 --group-col col_02 --method winsorize_5pct
```

## 单元测试

```bash
pytest -v skills/skill_2_stat_mcp/tests/test_skill_2.py
```

## 真实 stdio 会话

在仓库根目录安装 `pip install -e .`。以下客户端使用同一 Python 解释器启动服务。宿主在启动参数中指定只读 CSV 和本次运行的 ID。工具请求只携带登记的 ID。

```python
import asyncio
from uuid import uuid4
from core.mcp_client import StatMCPClient

async def inspect():
    async with StatMCPClient(
        data_path="data/fixtures/case_03_promo_confounding.csv",
        dataset_id="case_03", run_id=f"inspect_{uuid4().hex}",
    ) as client:
        print([tool["name"] for tool in await client.list_tools()])
        evidence = await client.call_tool("inspect_data", {"dataset_id": "case_03"})
        print(evidence["evidence_id"], client.timings)

asyncio.run(inspect())
```

`get_evidence` 读取当前子进程内的账本。退出后，证据保存在配置的输出目录下的 `<run_id>/evidence_ledger.jsonl`。重复使用已经落盘的运行 ID 会在写入时被拒绝。

`pytest -v tests/test_mcp_protocol.py` 覆盖真实协议的工具发现、Case 03/04 效应量、Case 06/07 输入隔离与证据查询。
