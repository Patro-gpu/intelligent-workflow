"""《数证 Audit》极简 Streamlit 过程观测台（<=250 行）。

启动命令：
    streamlit run app/dashboard.py
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st  # noqa: E402

from core.llm_router import LLMRouter  # noqa: E402
from core.schema import BlindReviewOutput, ClaimSpec, ReviewPosition  # noqa: E402
from core.security import SecurityError, sandbox_root  # noqa: E402
from skills.skill_1_data_profiler.skill import load_sandbox_config  # noqa: E402
from skills.skill_3_claim_planner.skill import freeze_claim, run_reviewer  # noqa: E402
from skills.skill_4_evidence_auditor.skill import check_invariants  # noqa: E402
from workflows.state_machine import AuditWorkflowRunner  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "data/fixtures"
INPUTS_DIR = REPO_ROOT / "workspace/inputs"

DEMO_CASES = {
    "Case 03: 大促混淆 / 辛普森悖论（+48 → 分层归零）": (
        FIXTURES_DIR / "case_03_promo_confounding.csv",
        "发放消费券使商户订单增加了 48 单。",
        "数据包含同期大促参与标记 is_promo，需检验混淆偏差。",
    ),
    "Case 04: 极端异常值敏感（+48 → 缩尾 +18）": (
        FIXTURES_DIR / "case_04_outlier_sensitivity.csv",
        "发券组商户订单量高于未发券组。",
        "需核验头部极端大店对均值的拉高效应。",
    ),
    "Case 05: 缺字段不可识别（主动止损 Insufficient）": (
        FIXTURES_DIR / "case_05_missing_confounder.csv",
        "发放消费券因果上使商户订单增加了 48 单。",
        "背景说明：同期存在大型促销活动，但当前导出表缺少大促参与字段。",
    ),
}


def _run_r2_vs_r5(csv_path: Path, raw_claim: str, context_notes: str, budget: float) -> tuple[dict, dict]:
    claim_spec = freeze_claim(raw_claim, claim_id="ui_claim", context_notes=context_notes)

    r2_router = LLMRouter(mock_mode=True, max_budget_cny=budget)

    def _single_only(c: ClaimSpec, prof, router):
        return run_reviewer("primary_reviewer", c, prof, router), BlindReviewOutput(
            claim_id=c.claim_id, position=ReviewPosition.UNCERTAIN, issues=[]
        ), False

    r2_res = AuditWorkflowRunner(
        router=r2_router, blind_review_fn=_single_only
    ).run(data_path=csv_path, raw_claim=raw_claim, claim_spec=claim_spec, persist_outputs=False)

    r5_router = LLMRouter(mock_mode=True, max_budget_cny=budget)
    r5_res = AuditWorkflowRunner(router=r5_router).run(
        data_path=csv_path, raw_claim=raw_claim, claim_spec=claim_spec, persist_outputs=True
    )
    return r2_res, r5_res


@contextmanager
def _uploaded_csv(upload):
    limit = int(load_sandbox_config()["max_file_size_mb"] * 1024 * 1024)
    data = upload.getbuffer()
    if len(data) > limit:
        raise SecurityError("上传文件超出 CSV 大小限制")
    root = sandbox_root(INPUTS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root) as directory:
        path = Path(directory) / "upload.csv"
        path.write_bytes(data)
        yield path


def main() -> None:
    st.set_page_config(page_title="数证 Audit 过程观测台", layout="wide")
    st.title("数证 Audit — 多 Agent 证据核验与确定性裁决观测台")
    st.caption("辩论发现待验证问题 · 白名单 MCP 确定性取证 · 规则引擎独立裁决")

    uploaded = None
    with st.sidebar:
        mode = st.radio("数据源模式", ["内置反转案例一键演示", "自定义上传 CSV"])
        budget = st.slider("单任务预算上限 (CNY)", 0.1, 5.0, 1.0, 0.1)

        if mode == "内置反转案例一键演示":
            selected = st.selectbox("选择内置案例", list(DEMO_CASES.keys()))
            csv_path, default_claim, default_ctx = DEMO_CASES[selected]
        else:
            uploaded = st.file_uploader("上传 CSV 文件", type=["csv"])
            default_claim = "发券组商户订单量高于未发券组。"
            default_ctx = ""
            csv_path = None

        claim_text = st.text_area("待审业务声明", value=default_claim, height=80)
        context_text = st.text_area("背景说明（可选）", value=default_ctx, height=70)
        run_btn = st.button("开始审计与 R2 vs R5 对比", type="primary", use_container_width=True)

    if not run_btn:
        st.info("请在左侧选择内置案例或上传 CSV，点击「开始审计与 R2 vs R5 对比」。")
        return
    if csv_path is None and uploaded is None:
        st.warning("请先上传 CSV 文件。")
        return

    try:
        if uploaded is not None:
            with _uploaded_csv(uploaded) as path:
                r2_res, r5_res = _run_r2_vs_r5(path, claim_text, context_text, budget)
        else:
            r2_res, r5_res = _run_r2_vs_r5(csv_path, claim_text, context_text, budget)
    except (SecurityError, ValueError) as exc:
        st.error(str(exc))
        return
    r2_v, r5_v, r5_state = r2_res["verdict"], r5_res["verdict"], r5_res["state"]
    inv_ok, violations = check_invariants(r5_state)

    col1, col2, col3 = st.columns(3)
    col1.metric("R2（单 Agent + MCP）裁决", r2_v.status.value.upper())
    col2.metric("R5（完整数证 Audit）裁决", r5_v.status.value.upper())
    col3.metric("6 条全局不变量", "全部通过" if inv_ok else f"告警 ({len(violations)})")

    st.subheader("6 条不变量合规徽章")
    limits = {"round": 2, "judge_calls": 1, "duplicate_tool_executions": 0,
              "invalid_evidence_accepted": 0, "judge_rule_overrides": 0, "tool_calls_after_terminal": 0}
    for column, (name, limit) in zip(st.columns(6), limits.items()):
        value = getattr(r5_state, name)
        display = column.success if value <= limit else column.error
        display(f"{name}={value} (≤{limit})")

    st.subheader("MCP 确定性证据账本")
    ev_rows = [
        {
            "evidence_id": ev.evidence_id,
            "tool_id": ev.tool_id,
            "scope_id": ev.scope_id,
            "sample_size": ev.sample_size,
            "result": json.dumps(ev.result, ensure_ascii=False),
        }
        for ev in r5_state.evidence.values()
    ]
    st.dataframe(ev_rows, use_container_width=True)

    tab_rep, tab_trace = st.tabs(["Markdown 审计报告", "状态机轨迹 JSON"])
    with tab_rep:
        st.markdown(r5_res["report_markdown"])
    with tab_trace:
        st.json(r5_res["trace"])


if __name__ == "__main__":
    main()
