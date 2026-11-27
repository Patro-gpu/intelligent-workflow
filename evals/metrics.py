"""4 组消融评测指标聚合模块（`evals.metrics`）。"""

from __future__ import annotations

from typing import Any, Dict, List


def compute_ablation_metrics(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按系统变体（R0 / R1 / R2 / R5）聚合准确率、误通过率、保守止损率、不变量通过率与成本。"""
    total = len(records)
    if total == 0:
        return {
            "total_cases": 0,
            "accuracy": 0.0,
            "false_verified_rate": 0.0,
            "insufficient_recall": 0.0,
            "invariants_pass_rate": 0.0,
            "avg_tool_calls": 0.0,
            "total_cost_cny": 0.0,
        }

    correct = sum(1 for r in records if r["predicted"] in r["allowed_verdicts"])

    non_valid_cases = [r for r in records if "verified" not in r["allowed_verdicts"]]
    false_verified = sum(1 for r in non_valid_cases if r["predicted"] == "verified")
    false_verified_rate = false_verified / len(non_valid_cases) if non_valid_cases else 0.0

    insuff_cases = [r for r in records if r["allowed_verdicts"] == ["insufficient"]]
    insuff_hits = sum(1 for r in insuff_cases if r["predicted"] == "insufficient")
    insuff_recall = insuff_hits / len(insuff_cases) if insuff_cases else 1.0

    inv_pass = sum(1 for r in records if r.get("invariants_passed", True))
    avg_tools = sum(int(r.get("tool_calls", 0)) for r in records) / total
    total_cost = sum(float(r.get("cost_cny", 0.0)) for r in records)

    return {
        "total_cases": total,
        "accuracy": round(correct / total, 4),
        "false_verified_rate": round(false_verified_rate, 4),
        "insufficient_recall": round(insuff_recall, 4),
        "invariants_pass_rate": round(inv_pass / total, 4),
        "avg_tool_calls": round(avg_tools, 2),
        "total_cost_cny": round(total_cost, 6),
    }


def render_ablation_markdown(summary_by_system: Dict[str, Dict[str, Any]]) -> str:
    """渲染 R0 / R1 / R2 / R5 消融对比 Markdown 表格。"""
    lines = [
        "# 《数证 Audit》4 组消融评测对比报告",
        "",
        "| 系统变体 | 样本数 | 裁决准确率 | 误判 Verified 率 (越低越好) | 缺口止损率 (`insufficient`) | 6 不变量通过率 | 平均工具调用 | 总费用 (CNY) |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for sys_name, m in summary_by_system.items():
        lines.append(
            f"| **{sys_name}** | {m['total_cases']} | {m['accuracy']:.1%} | "
            f"{m['false_verified_rate']:.1%} | {m['insufficient_recall']:.1%} | "
            f"{m['invariants_pass_rate']:.1%} | {m['avg_tool_calls']:.2f} | {m['total_cost_cny']:.6f} |"
        )
    lines.append("")
    return "\n".join(lines)
