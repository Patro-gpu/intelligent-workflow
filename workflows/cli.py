#!/usr/bin/env python3
"""《数证 Audit》主工作流 `/audit` 命令行入口。

示例：
    python -m workflows.cli audit \
        --data data/fixtures/case_03_promo_confounding.csv \
        --claim "发放消费券使商户订单增加了 48 单。" \
        --trace --budget 1.0
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.llm_router import LLMRouter  # noqa: E402
from workflows.state_machine import AuditWorkflowRunner  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shuzheng Audit bounded state-machine CLI (/audit).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_p = subparsers.add_parser("audit", help="Audit a business claim against a CSV dataset.")
    audit_p.add_argument("--data", required=True, help="Path to CSV inside allowed read roots")
    audit_p.add_argument("--claim", required=True, help="Natural-language claim to verify")
    audit_p.add_argument("--context", default="", help="Optional context notes")
    audit_p.add_argument("--budget", type=float, default=1.0, help="Hard task budget in CNY (default: 1.0)")
    audit_p.add_argument("--trace", action="store_true", help="Print execution trace summary")
    audit_p.add_argument("--enable-final-judge", action="store_true", help="Enable optional blinded final judge")
    audit_p.add_argument("--live", action="store_true", help="Use live LLM APIs (default: MOCK_MODE=true)")
    audit_p.add_argument("--run-id", default=None, help="Optional run identifier")

    args = parser.parse_args(argv)
    if args.command == "audit":
        router = LLMRouter(mock_mode=not args.live, max_budget_cny=args.budget)
        runner = AuditWorkflowRunner(
            router=router,
            final_judge_enabled=args.enable_final_judge,
        )
        result = runner.run(
            data_path=args.data,
            raw_claim=args.claim,
            context_notes=args.context,
            run_id=args.run_id,
            budget_cny=args.budget,
            persist_outputs=True,
        )
        verdict = result["verdict"]
        print(result["report_markdown"])
        if args.trace:
            print("\n=== Execution Trace ===")
            print(json.dumps(result["trace"], indent=2, ensure_ascii=False))
        print(f"\nOutputs saved to: {result['output_dir']} | Verdict: {verdict.status.value}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
