# 数证 Audit（Shuzheng Audit）

面向数字经济场景的多 Agent 数据结论审计与证据验证系统。

> **核心定位**：ThinkClaw 让多个 AI 通过辩论形成意见；数证 Audit 让多个 AI 通过辩论发现需要验证的问题，再由可追溯证据和确定性规则决定结论是否成立。

## 当前进度

- **阶段 A（起盘与核心契约冻结）**：进行中 / 即将完成
  - 全局守则 `AGENTS.md` 与架构单点真相源 `docs/BLUEPRINT_v2.md`
  - 冻结 Pydantic v2 数据契约 `core/schema.py`（`extra="forbid"`）
  - 权限与安全模块 `core/security.py`（路径沙箱、只读数据集注册表、安全列 ID、公式注入防护）
  - 多模型路由与预算熔断 `core/llm_router.py`（支持 `MOCK_MODE=true` 与 `Qwen` 降级）
  - 7 份确定性基准数据集与真值标注 `data/fixtures/manifest.json`
  - 密钥防泄露脚本 `scripts/check_secrets.py` 与模型连通性测试 `scripts/ping_models.py`
- **阶段 B（核心 Skill、状态机、消融评测与观测台）**：见 [`HANDOFF.md`](./HANDOFF.md)

## 快速验证（离线 Mock 模式）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
pytest -v
python scripts/check_secrets.py
MOCK_MODE=true python scripts/ping_models.py
```

## 目录概览

- `core/`：数据契约、安全权限层、多模型路由、MCP 客户端占位
- `configs/`：模型路由与阈值配置、MCP 服务配置、外置提示词目录
- `skills/`：4 个独立可复用个人 Skill 模块
- `workflows/`：6 阶段有界状态机与 `/audit` 入口（阶段 B3）
- `data/`：确定性基准数据生成器与标准 `fixtures/`
- `evals/`：4 组消融评测脚本（阶段 B4）
- `app/`：极简 Streamlit 过程观测台（阶段 B4）
- `scripts/`：安全扫描与模型连通性实测脚本
- `tests/`：冒烟测试、核心契约测试与验收测试
