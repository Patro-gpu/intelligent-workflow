# Skill 1: 数据质量与语义体检（`skill_1_data_profiler`）

- **负责人**：成员 1
- **独立 CLI 入口**：`/inspect`（阶段 B1 实现）
- **职责**：调用 `core/security.py` 只读注册 CSV，输出 `DatasetProfile`（安全列 ID、缺失率、非有限数值告警、候选分组列）。
