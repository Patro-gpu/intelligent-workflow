# Skill 1: 数据质量与语义体检（`skill_1_data_profiler`）

- **负责人**：成员 1
- **独立 CLI 入口**：`inspect`（对应 `/inspect`）
- **核心能力**：
  1. 通过 `core/security.py` 的 `DatasetRegistry` 校验白名单只读目录，物理拦截 `../` 路径穿越与符号链接逃逸。
  2. 自动将所有列名映射为安全内部 ID（`col_01`, `col_02`, ...），隔离恶意提示词表头并检测公式注入单元格。
  3. 输出冻结契约 `DatasetProfile`（含 `dataset_hash`、`candidate_group_cols`、`quality_warnings`）以及脱敏列级统计摘要（缺失率、分位数、重复行、IQR 极值计数）。

## 独立运行示例

```bash
python -m skills.skill_1_data_profiler.cli inspect \
  --data data/fixtures/case_03_promo_confounding.csv
```

## 单元测试

```bash
pytest -v skills/skill_1_data_profiler/tests/test_skill_1.py
```
