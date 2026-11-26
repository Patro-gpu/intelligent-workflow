# Skill 3: 命题冻结与双轮增量验证规划（`skill_3_claim_planner`）

- **负责人**：成员 3
- **独立 CLI 入口**：`challenge`（对应 `/challenge`）
- **核心能力**：
  1. **命题冻结（`freeze_claim`）**：将自然语言声明结构化为 `ClaimSpec`（支持 `descriptive`、`direction`、`magnitude`、`universal`、`causal` 五类，硬限最多 `1` 主 + `2` 子命题）。
  2. **第 1 轮并行盲审（`run_blind_review`）**：基于 `configs/prompts/primary_reviewer.md` 与 `challenger.md` 隔离运行初审与反方，自动透传 `model_diversity_lost`。
  3. **去重与白名单任务规划（`deduplicate_and_plan_tasks`）**：基于 `compute_issue_key` / `compute_request_key` 去重，过滤非白名单工具。
  4. **第 2 轮增量验证与防复读守卫（`evaluate_cross_review_deltas`）**：仅接受 `ReviewAction` 五枚举，拦截重复检验与无证据妥协，自动触发 `NO_PROGRESS`。

## 独立运行示例（离线 Mock 模式）

```bash
python -m skills.skill_3_claim_planner.cli challenge \
  --data data/fixtures/case_03_promo_confounding.csv \
  --claim "发放消费券使商户订单增加了 48 单。" \
  --context "数据包含同期大促参与标记 is_promo"
```

## 单元测试

```bash
pytest -v skills/skill_3_claim_planner/tests/test_skill_3.py
```
