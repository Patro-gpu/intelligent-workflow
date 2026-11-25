# AGENTS.md — 《数证 Audit（Shuzheng Audit）》全局工程守则

> 本文档适用于所有协作开发本仓库的 AI 编程助手（Gemini、GPT 6.2 Astra、Codex 等）与人类成员。在修改任何代码前必须完整阅读并严格遵守以下红线。

## 1. 项目定位
- **项目名称**：数证 Audit（Shuzheng Audit）——面向数字经济场景的多 Agent 数据结论审计与证据验证系统。
- **核心答辩定位**：*ThinkClaw 让多个 AI 通过辩论形成意见；数证 Audit 让多个 AI 通过辩论发现需要验证的问题，再由可追溯证据和确定性规则决定结论是否成立。*

## 2. 六条不可违背的全局开发红线

1. **三权分立原则**
   - **辩论权（Agents）**：初审（`primary_reviewer`）与反方（`challenger`）只提出结构化待验证问题与枚举动作，无权决定最终结论。
   - **取证权（5 个白名单统计 MCP）**：纯确定性 Python 统计计算并返回唯一 `evidence_id`，严禁 `eval` / `exec` / `DataFrame.query` 或模型自由执行代码。
   - **裁决权（确定性规则引擎）**：最终状态仅由纯确定性函数 `classify(state)` 生成（`verified` | `limited` | `contradicted` | `insufficient`）。可选昂贵独立终裁模型默认关闭（`FINAL_JUDGE_ENABLED=False`），每任务最多调用 `1` 次，仅可提出可机械核验的证据映射修正，**绝不允许覆盖硬规则或输出 `final_status`**。

2. **工具权限层物理安全**
   - 源 CSV 列名在体检阶段映射为内部安全 ID（`col_01`, `col_02`, ...），工具层仅接受已登记 `dataset_id`、`col_id` 与枚举参数，不接受任意路径、SQL 或代码表达式。
   - 源数据目录只读，输出目录锁定 `./workspace/outputs/`，强制 `.resolve()` 路径规范化防 `../` 路径穿越与符号链接逃逸；限制文件大小（默认 `10 MB`）与行列数（默认 `100,000` 行、`100` 列）；导出电子表格字段防公式注入（转义 `=`, `+`, `-`, `@`, `\t`, `\r` 开头内容）。
   - 严禁任何真实 API Key、Token、中转地址或敏感原始数据进入代码、测试或 Git 历史。

3. **模型双轨与诚实降级**
   - 主力异构双轨模型为 **DeepSeek**（初审）与 **火山引擎豆包**（反方）；备用降级模型为 **Qwen**。
   - 若双方主力同时降级为同一模型，必须记录 `model_diversity_lost=True`，绝不虚报异构验证。
   - 可选昂贵终裁模型默认关闭。

4. **离线可测与预算硬上限**
   - `core/llm_router.py` 必须支持 `MOCK_MODE=true`，所有单元测试与验收测试在离线 Mock + 本地真实 MCP 下即可跑通。
   - 6 条全局硬不变量：
     - 辩论轮次 `round <= 2`
     - 终裁调用 `judge_calls <= 1`
     - 重复工具执行 `duplicate_tool_executions == 0`
     - 非法证据采纳 `invalid_evidence_accepted == 0`
     - 终裁覆盖硬规则 `judge_rule_overrides == 0`
     - 终态后工具调用 `tool_calls_after_terminal == 0`

5. **4 个独立可复用个人 Skill**
   - `skills/` 下 4 个模块分别对应 4 位成员的个人项目，每个 Skill 均支持独立 CLI 运行（`/inspect`、`/verify`、`/challenge`、`/report`），脱离主工作流即可单独复用与评测。

6. **真实 MCP 协议通信**
   - `skills/skill_2_stat_mcp` 必须通过官方 `mcp` / `fastmcp` 启动标准 `stdio` MCP Server，主工作流通过真实 MCP Client 完成 `list_tools` 与 `call_tool`，严禁直接 import Python 函数伪装成 MCP。

## 3. 分支与提交规范
- `main`：稳定发布分支，禁止直接强推（严禁 `git push --force`）。
- `dev`：日常集成分支，所有阶段性功能从 `dev` 切出 `feat/<name>` 分支开发，完成后经 `pytest` 与 `python scripts/check_secrets.py` 全绿后以 `--no-ff` 合并回 `dev`。
- `core/schema.py` 为全局冻结契约，所有 Pydantic 模型保持 `extra="forbid"`，阶段 B 严禁破坏已有字段兼容性。
- 每次提交前必须执行：
  ```bash
  pytest -v
  python scripts/check_secrets.py
  ```
