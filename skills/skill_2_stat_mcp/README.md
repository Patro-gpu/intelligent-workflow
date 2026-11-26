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
