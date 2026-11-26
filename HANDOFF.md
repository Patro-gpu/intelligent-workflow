# HANDOFF.md — 《数证 Audit》阶段 A → 阶段 B 交接看板

> **当前里程碑**：`v0.1-contracts-frozen`（阶段 A 全部完成，等待阶段 B 接力）
> **单点真相源**：[`docs/BLUEPRINT_v2.md`](./docs/BLUEPRINT_v2.md) ｜ **工程红线**：[`AGENTS.md`](./AGENTS.md)

---

## 一、阶段 A（U1 & U2）完成清单

- [x] **A1 — 仓库骨架与安全基线（已提交至 `main`，切出 `dev`）**
  - `AGENTS.md`：6 条全局工程红线与分支规范
  - `docs/BLUEPRINT_v2.md`：完整 V2 架构、5 个 MCP 工具、6 阶段状态机与 T01–T12 验收规范
  - `pyproject.toml`、`.gitignore`、`.env.example`、`configs/models.yaml`、`configs/mcp_servers.yaml`
  - `scripts/check_secrets.py`：提交前密钥防泄露扫描（0 告警通过）
  - `core/mcp_client.py`：阶段 B1 真实 `stdio` MCP 客户端留桩
  - `skills/skill_1..4`、`workflows/`、`evals/`、`app/`、`configs/prompts/` 目录树与占位文档
- [x] **A2 — 冻结 V2 数据契约、权限安全层、多模型路由与 7 份基准集（`feat/u2-core-contracts-and-fixtures` → `--no-ff` 合并入 `dev`）**
  - `core/schema.py`：全局 Pydantic v2 数据契约（全部 `extra="forbid"`）、5 个枚举、`canonical_json`、`compute_request_key`、`compute_issue_key`、`FinalJudgeOutput` 联合类型（严禁 `final_status`）与完整 `WorkflowState`
  - `core/security.py`：`resolve_safe_path`（防 `../` 与符号链接逃逸）、`sanitize_cell_for_export`（防公式注入）、`DatasetRegistry`（只读加载、大小/行列上限、`col_01..` 安全列 ID、恶意表头与 `NaN`/`Inf` 预警）
  - `core/llm_router.py` & `scripts/ping_models.py`：OpenAI 兼容多模型路由、`MOCK_MODE=true` 离线可测、预算硬上限熔断（`BudgetExceededError`）、`Qwen` 降级与 `model_diversity_lost` 检测、脱敏 JSONL 审计日志
  - `data/generate_benchmark.py` & `data/fixtures/`：固定 `seed=42` 生成 7 份标准测试 CSV 与 `manifest.json`（精准验证 Case 03 总均值 154 vs 106 差 48 且分层差为 0；Case 04 原始差 48 且 5% 缩尾衰减 > 50%）
  - `tests/test_smoke.py` + `tests/test_core_contracts.py`：**15 / 15 `pytest` 全部通过**
- [x] **B2 — 外置提示词模板、独立 Skill 3（命题冻结与双轮规划）与独立 Skill 4（确定性裁决引擎）（`feat/step-4-planner-and-auditor` → `--no-ff` 合并入 `dev`）**
  - `configs/prompts/`：`claim_parser.md`、`primary_reviewer.md`、`challenger.md`、`final_judge.md`（严格三权分立）
  - `skills/skill_3_claim_planner/`：`skill.py`、`cli.py`（`/challenge`）、`README.md`、`tests/test_skill_3.py`
  - `skills/skill_4_evidence_auditor/`：`skill.py`、`cli.py`（`/report`）、`README.md`、`tests/test_skill_4.py`
  - 全量 `pytest -v`（自动发现 `tests/` + `skills/`）：**26 / 26 全部通过（0 告警）**，密钥扫描 **0 告警**
- [x] **B1（核心部分）— 独立 Skill 1（数据体检）+ Skill 2（5 个白名单确定性统计引擎、FastMCP stdio 包装与 `/verify` CLI）（`feat/step-3-profiler-and-engine` → `--no-ff` 合并入 `dev`）**
  - `skills/skill_1_data_profiler/`：`skill.py`、`cli.py`（`/inspect`）、`README.md`、`tests/test_skill_1.py`
  - `skills/skill_2_stat_mcp/`：`engine.py`（5 个确定性工具 + `EvidenceRecord` 账本持久化）、`server.py`（官方 `FastMCP` stdio 包装，待 Python 3.11+ 环境安装 `mcp` 后直接运行）、`cli.py`（`/verify`）、`README.md`、`tests/test_skill_2.py`
  - 全量 `pytest -v`：**36 / 36 全部通过**，密钥扫描 **0 告警**（仅余 `core/mcp_client.py` 与 `tests/test_mcp_protocol.py` 需在 Python 3.11+ 下联调真实 stdio 协议）
- [x] **B3 — 6 阶段有界状态机、`/audit` 命令行入口与 T01–T12 全套验收（`feat/step-5-state-machine-and-acceptance` → `--no-ff` 合并入 `dev`）**
  - `workflows/state_machine.py`：6 阶段有界状态机、`should_escalate()` 12 项门控、`build_blinded_judge_input()` 盲化随机排序、同 `request_key` 零重复执行、`review_closed`/`judge_closed` 不可逆守卫、自动落盘 `report.md`/`verdict.json`/`trace.json`
  - `workflows/cli.py`：`/audit` 风格 CLI 入口
  - `tests/test_verdict_acceptance.py`：**T01–T12 全部 12 项验收 + 6 条全局强制不变量 + `/audit` 端到端测试通过**
  - 全量 `pytest -v`：**49 / 49 全部通过**，密钥扫描 **0 告警**

> **环境说明**：阶段 A 起盘机默认系统 Python 为 `3.9.6`，已在 `.venv` 安装 `pydantic>=2.0`, `pandas`, `numpy`, `scipy`, `pyyaml`, `python-dotenv`, `openai`, `pytest` 并跑通全量测试。`mcp` 官方包要求 Python `>=3.10`（`pyproject.toml` 声明 `>=3.11`），请阶段 B 开发者在 **Python 3.11+** 环境中执行 `pip install -e ".[ui]"` 一次性安装全部依赖（含 `mcp` 与 `streamlit`）。

---

## 二、阶段 B（同伴 B：`GPT 6.2 Astra`）快速上手

```bash
git clone https://github.com/Patro-gpu/intelligent-workflow.git
cd intelligent-workflow
git checkout dev
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[ui]"
pytest -v
python scripts/check_secrets.py
```

### 阶段 B 接力提示词（按序执行 B1 → B4）

#### Prompt B1（Step 3）：独立 Skill 1 + 真实 FastMCP 服务（Skill 2）
```text
请先完整阅读仓库根目录的 AGENTS.md、HANDOFF.md、docs/BLUEPRINT_v2.md 以及已冻结的 core/schema.py 和 core/security.py。切勿破坏 core/schema.py 已有字段兼容性。
基于 dev 切出分支 `feat/step-3-profiler-and-mcp`，执行【阶段 B - B1】：
1. 开发独立可复用模块 `skills/skill_1_data_profiler/`（含 skill.py, cli.py, README.md, tests/test_skill_1.py）：调用 core/security.py 注册只读 CSV，输出 DatasetProfile，支持独立命令行 `/inspect` 风格调用。
2. 开发独立可复用模块 `skills/skill_2_stat_mcp/`（含 server.py, engine.py, cli.py, README.md, tests/test_skill_2.py）：使用官方 mcp/fastmcp 通过真实 stdio 协议暴露 5 个确定性白名单工具（inspect_data, aggregate, group_compare, sensitivity_check, get_evidence）；严禁 eval/exec/query；按规范化 request_key 生成确定性 EvidenceRecord 并持久化到本次运行证据账本；缺失统计量严格返回 null。
3. 实现 `core/mcp_client.py`：通过真实 MCP stdio 协议拉起 server.py，完成 list_tools 与 call_tool 并记录耗时。
4. 编写 `tests/test_mcp_protocol.py`：真实协议发现 5 个工具，在 Case 03 验证分层差值从 48 变为 0，在 Case 04 验证 sensitivity_check 暴露相对变化率，在 Case 06/07 验证恶意列名隔离与非法参数/越权路径被工具层硬拒绝。
5. 更新 HANDOFF.md，运行全量 pytest 与 python scripts/check_secrets.py 全部绿灯后合并入 dev 并推送 GitHub。
```

#### Prompt B2（Step 4）：独立 Skill 3（命题冻结与规划）+ 独立 Skill 4（确定性裁决引擎）
```text
请阅读 AGENTS.md、docs/BLUEPRINT_v2.md、core/schema.py 与已有 skills/ 代码。
基于 dev 切出分支 `feat/step-4-planner-and-auditor`，执行【阶段 B - B2】：
1. 在 `configs/prompts/` 下编写外置模板：claim_parser.md, primary_reviewer.md, challenger.md, final_judge.md（贯彻三权分立）。
2. 开发独立模块 `skills/skill_3_claim_planner/`（skill.py, cli.py, README.md, tests/test_skill_3.py）：实现命题冻结（5 类 ClaimType，最多 1 主 + 2 子命题）、第 1 轮并行盲审与第 2 轮增量验证规划（ReviewAction 5 枚举，防复读）、request_key/issue_key 去重、支持 MOCK_MODE 与 `/challenge` CLI。
3. 开发独立模块 `skills/skill_4_evidence_auditor/`（skill.py, cli.py, README.md, tests/test_skill_4.py）：实现确定性规则核验、灰色证据相对变化率判定、纯确定性 `classify(state)`、复合命题合取保护、`validate_judge_output(output, context)` 机械校验，以及绑定 evidence_id 的 Markdown 审计报告生成（`/report` CLI）。
4. 编写充分单元测试覆盖 +48/+18 缩尾敏感、子组反向、因果缺口与越权拦截。
5. 更新 HANDOFF.md，跑通全量 pytest 与 check_secrets.py 后合并入 dev 并推送 GitHub。
```

#### Prompt B3（Step 5）：6 阶段有界状态机、`/audit` 入口与 T01–T12 验收测试
```text
请阅读 AGENTS.md、docs/BLUEPRINT_v2.md 及 core/、skills/ 全部代码。
基于 dev 切出分支 `feat/step-5-state-machine-and-acceptance`，执行【阶段 B - B3】：
1. 实现 `workflows/state_machine.py`：严格按 docs/BLUEPRINT_v2.md 实现 6 阶段有界状态机、Qwen 降级与 `model_diversity_lost=True` 标记、不可逆关闭标记 `review_closed`/`judge_closed`、同 request_key 零重复执行、`should_escalate(state)` 严控门控、盲化终裁上下文构造、以及关键缺口/无进展/预算耗尽主动停止。
2. 实现 `workflows/cli.py`：支持 `/audit` 风格指令（如 `python -m workflows.cli audit --data ... --claim ... --trace --budget 1.0`），输出报告与轨迹 JSON 至 `workspace/outputs/<run_id>/`。
3. 编写 `tests/test_verdict_acceptance.py`：用 Mock LLM + 真实本地 5 个 FastMCP 工具完整通过 **T01–T12** 全部 12 项验收与 **6 条强制不变量断言**。
4. 更新 HANDOFF.md，全量 pytest 与 check_secrets.py 绿灯后合并入 dev 并推送 GitHub。
```

#### Prompt B4（Step 6 & Step 7）：4 组消融评测、极简观测看板与答辩收口
```text
请阅读 AGENTS.md、docs/BLUEPRINT_v2.md 及现有全部代码。
基于 dev 切出分支 `feat/step-6-7-evals-and-dashboard`，执行【阶段 B - B4】：
1. 扩展 `data/generate_benchmark.py` 支持 `--scale full` 生成 20 份分层评测集（3 正常、3 口径分母错、4 混淆/辛普森悖论、4 异常值/子组敏感、4 缺字段/缺因果不可识别、2 安全红队）与完整 manifest.json。
2. 实现 `evals/metrics.py` 与 `evals/run_ablation.py`：实现 R0（固定脚本）、R1（单模型裸答）、R2（强单 Agent + 5 MCP）、R5（完整数证 Audit，含 `--enable-final-judge` 可选开关），输出 Markdown 对比表与 CSV。
3. 实现极简 Streamlit 过程观测台 `app/dashboard.py`（<=250 行）：支持内置反转案例一键演示、自定义上传、R2 vs R5 对比、MCP 证据账本核对与 6 条不变量合规徽章。
4. 完善根目录 `README.md`（Mermaid 架构图、ThinkClaw 升级对比、4 个独立个人 Skill 运行指令、消融实验指令、《5 分钟随机抽签答辩速查提纲》）。
5. 更新 HANDOFF.md，全量 pytest、MOCK_MODE 20 份消融评测与 check_secrets.py 全绿后合并入 dev，发起最终发布至 main 并推送 GitHub。
```

---

## 三、成员 3 & 成员 4 并行分支认领说明

| 成员 | 建议认领分支（基于 `dev`） | 核心职责 |
| :--- | :--- | :--- |
| **成员 3**（Skill 3 主理人） | `feat/member3-prompts-and-skill3-docs` | 打磨 `configs/prompts/` 下 DeepSeek 与豆包的结构化 JSON 输出稳定性，跑角色互换小样本实测，完善 `skills/skill_3_claim_planner/README.md` |
| **成员 4**（Skill 4 & 评测安全主理人） | `feat/member4-evals-and-skill4-docs` | 审核 20 份分层基准题库，实测 `Qwen` 故障降级与安全红队，执行真实 API 4 组消融实验并整理报表，完善 `skills/skill_4_evidence_auditor/README.md` |
