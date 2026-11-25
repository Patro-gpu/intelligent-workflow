# 《数证 Audit（Shuzheng Audit）》V2 完整架构与阶段 B 验收规范

> 本文档为本仓库单点真相源（Single Source of Truth），供阶段 B 接力开发与成员 3、成员 4 离线查阅。

## 一、核心设计理念与三权分立

* **核心定位**：ThinkClaw 让多个 AI 通过辩论形成意见；数证 Audit 让多个 AI 通过辩论发现需要验证的问题，再由可追溯证据和确定性规则决定结论是否成立。
* **辩论权（Agents）**：`primary_reviewer`（DeepSeek）与 `challenger`（火山引擎豆包）独立提出结构化待验证问题与枚举动作，无权决定最终裁决。
* **取证权（5 个白名单统计 MCP）**：纯确定性 Python 统计计算并返回唯一 `evidence_id`，严禁 `eval`/`exec`/`query` 或自由代码执行。
* **裁决权（确定性规则引擎）**：最终状态仅由纯确定性函数 `classify(state)` 生成（`verified` | `limited` | `contradicted` | `insufficient`）。可选昂贵终裁模型默认关闭（`FINAL_JUDGE_ENABLED=False`），每任务最多调用 `1` 次，仅可提出可机械核验的证据映射修正，绝不允许覆盖硬规则或输出 `final_status`。

---

## 二、5 个白名单统计 MCP 工具规格（`skills/skill_2_stat_mcp`）

通过官方 `mcp` / `fastmcp` 以真实 `stdio` 协议暴露，严禁直接 import Python 函数伪装 MCP；所有参数仅接受已登记 `dataset_id`、`col_id` 与枚举值；缺失统计量严格返回 `null`：

1. `inspect_data(dataset_id)`：行列数、安全列映射、缺失率、分位数、重复行与极值预警。
2. `aggregate(dataset_id, target_col_id, metric, filters=None)`：`metric` 限定 `mean | median | sum | count | rate`。
3. `group_compare(dataset_id, target_col_id, group_col_id, metric, stratify_col_id=None)`：组间差值、置信区间/p 值参考；支持 `stratify_col_id` 分层复算（专克辛普森悖论）。
4. `sensitivity_check(dataset_id, target_col_id, group_col_id, method)`：`method` 限定 `winsorize_5pct | trim_top_1pct | median_vs_mean | drop_outliers_iqr`，计算相对变化率 $D = \frac{|\beta_1 - \beta_0|}{\max(|\beta_0|, \epsilon)}$ 并保留原始结果。
5. `get_evidence(run_id, evidence_id=None)`：按 ID 或全量拉取本次运行的确定性证据账本。

---

## 三、6 阶段有界状态机与硬守卫（`workflows/state_machine.py`）

1. **`INIT`**：校验输入与单位，冻结 `ClaimSpec`、数据哈希、规则阈值（`0.20 / 0.50 / 0.05`）与必要检验；输入不明确直接终止为 `insufficient`。
2. **`BLIND_REVIEW`（第 1 轮并行盲审）**：`primary_reviewer` 与 `challenger` 仅接收相同 `ClaimSpec` 与 `DatasetProfile`（隔离原始诱导报告、彼此不可见），独立输出 `BlindReviewOutput`。主力故障按预算降级 `qwen`；若双方均降级为同一模型，置 `model_diversity_lost=True`。
3. **`PLAN_AND_RUN_MCP`**：合并去重任务（基于 `request_key`）；越权/非法参数直接拒绝但不删除必要缺口；命中已执行缓存直接复用证据；预算与调用次数内经真实 MCP Client 执行并写入证据账本。
4. **`RULE_CHECK`**：由确定性程序计算状态布尔位并按优先级路由：
   * 有效决定性反证 → 立即终止 `contradicted`；
   * 存在合法、必要、未处理检验且预算允许 → 回 `PLAN_AND_RUN_MCP`（不加辩论轮次）；
   * 必要检验全完且完整支持无重大局限 → 提前终止 `verified`；核心支持且局限明确无未决重大冲突 → 提前终止 `limited`；
   * 关键数据/识别缺口无法补齐 → 立即终止 `insufficient`（禁止唤醒昂贵模型）；
   * 第 1 轮存在可工具验证的重大争议、辩论未关闭且预算充足 → 冻结快照，`round = 2`，进入 `CROSS_REVIEW`；
   * 满足 `should_escalate(state)`（`FINAL_JUDGE_ENABLED and not judge_closed and judge_calls < 1 and required_checks_complete and not critical_gap and unresolved_conflict and not decisive_refutation and conflict_type == "evidence_mapping" and conflict_is_material and cheap_resolution_exhausted and verification_path_exists and not pending_tests and remaining_budget >= reserved_judge_cost`）→ 原子计数、预留预算、置 `judge_closed=True`，盲化进入 `FINAL_JUDGE`；
   * 其余情况 → 进入 `FINALIZE`。
5. **`CROSS_REVIEW`（第 2 轮增量验证） / `FINAL_JUDGE`（盲化复核）**：
   * `CROSS_REVIEW`：仅允许提交 `ReviewAction` 5 种枚举增量；复读/无进展/无证据妥协记 `NO_PROGRESS`，置 `review_closed=True` 回 `RULE_CHECK`；合法增量入队后置 `review_closed=True` 去 `PLAN_AND_RUN_MCP`。
   * `FINAL_JUDGE`：输入剥离模型名、发言顺序（候选可复现随机排序）、自由文本、人数与置信度；输出经 `validate_judge_output()` 机械核验通过后才更新映射回 `RULE_CHECK`，否则拒绝进 `FINALIZE`。
6. **`FINALIZE`**：执行纯确定性 `classify(state)`（先判 `decisive_refutation` → 再判 `critical_gap / unresolved_conflict / not required_checks_complete` → 再判 `full_claim_supported and not material_limitation` → 再判 `core_supported and material_limitation` → 默认 `insufficient`）。

---

## 四、必过验收测试 T01–T12 与 6 条不变量断言（`tests/test_verdict_acceptance.py`）

* **T01** 双方一致且必要检验通过 → 第 1 轮 `verified`
* **T02** 双方一致但有效 MCP 存在决定性反证 → `contradicted`（忽略模型共识）
* **T03** 原始 `+48`、缩尾后 `+18`（衰减 `62.5%`），方向命题且稳健性属审计要求 → `limited`
* **T04** 全称正向命题存在可靠负向子组反例 → `contradicted`
* **T05** 第 2 轮双方仅复读 → 不重复执行工具，关闭辩论
* **T06** 关键字段缺失 → `insufficient`，昂贵调用 `= 0`
* **T07** 终裁引用不存在的 `evidence_id` → 拒绝输出
* **T08** 终裁试图覆盖硬规则或输出 `final_status` → 拒绝越权
* **T09** 重复请求或预算耗尽 → 有限终止且不伪造证据
* **T10** 双方降级同一模型（Qwen）→ 标记 `model_diversity_lost=True`
* **T11** 无法客观核验终裁修正 → 保留未决冲突，必要时 `insufficient`
* **T12** 复合命题中因果识别不足 → 不将方向通过误判为整体 `verified`

**6 条强制不变量断言**：
```python
assert state.round <= 2
assert state.judge_calls <= 1
assert state.duplicate_tool_executions == 0
assert state.invalid_evidence_accepted == 0
assert state.judge_rule_overrides == 0
assert state.tool_calls_after_terminal == 0
```

---

## 五、并行协作分工

* **同伴 B（阶段 B1–B4）**：依次实现 Skill 1 + 真实 FastMCP Skill 2（B1）、Skill 3 + Skill 4（B2）、6 阶段状态机与 T01–T12 验收（B3）、4 组消融评测与极简 Streamlit 观测台（B4）。
* **成员 3（开源轨 + Skill 3 主理人）**：拉取 `dev` 后负责打磨 `configs/prompts/` 下针对 DeepSeek 与豆包的结构化 JSON 输出稳定性，跑一次角色互换小样本实测，完善 `skills/skill_3_claim_planner/README.md`。
* **成员 4（开源轨 + Skill 4 & 评测安全主理人）**：拉取 `dev` 后负责审核 20 份分层基准题库、跑通 `Qwen` 故障降级与安全红队实测、执行真实 API 4 组消融实验并整理报表，完善 `skills/skill_4_evidence_auditor/README.md`。
