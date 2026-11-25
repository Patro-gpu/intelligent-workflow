# Skill 3: 命题冻结与双轮增量验证规划（`skill_3_claim_planner`）

- **负责人**：成员 3
- **独立 CLI 入口**：`/challenge`（阶段 B2 实现）
- **职责**：命题冻结（5 类 `ClaimType`，最多 1 主 + 2 子命题）、第 1 轮并行盲审与第 2 轮增量验证规划（`ReviewAction` 5 枚举，防复读）、基于 `request_key`/`issue_key` 去重。
