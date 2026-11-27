# Skill 4: 确定性规则裁决与证据链核验报告（`skill_4_evidence_auditor`）

- **负责人**：成员 4
- **独立 CLI 入口**：`report`（对应 `/report`）
- **核心能力**：
  1. **稳健性衰减判定（`assess_robustness`）**：按 $D = \frac{|\beta_1 - \beta_0|}{\max(|\beta_0|, \epsilon)}$ 与阈值 `0.20 / 0.50` 区分 `stable` / `warning` / `severe`。
  2. **确定性规则核验（`evaluate_evidence_rules`）**：支持口径分母反证、辛普森悖论分层归零反证、全称子组反例、缩尾敏感性局限以及**复合命题合取保护**（因果缺口绝不误判为 `verified`）。
  3. **纯确定性裁决（`classify(state)`）**：严格输出 `verified` | `limited` | `contradicted` | `insufficient`。
  4. **终裁输出机械校验（`validate_judge_output`）**：拦截不存在的 `evidence_id`、非法候选 ID 以及试图塞入 `final_status` 覆盖硬规则的越权行为。
  5. **可追溯报告（`render_markdown_report`）**：所有结论绑定 `[evidence_id]` 并附带 6 条不变量合规表。

## 独立运行示例

```bash
python -m skills.skill_4_evidence_auditor.cli report --input path/to/state_bundle.json
```

## 单元测试

```bash
pytest -v skills/skill_4_evidence_auditor/tests/test_skill_4.py
```
