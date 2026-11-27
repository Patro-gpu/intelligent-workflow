#!/usr/bin/env python3
"""Skill 4 独立命令行入口（`/report` 风格）。

接收包含 `claim_spec` 与 `workflow_state` 的 JSON 文件，执行确定性规则核验、
`classify(state)` 裁决，并输出绑定 `evidence_id` 的 Markdown 审计报告。

示例：
    python -m skills.skill_4_evidence_auditor.cli report --input bundle.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.security import resolve_safe_path, sandbox_root
from core.schema import ClaimSpec, WorkflowState  # noqa: E402
from skills.skill_4_evidence_auditor.skill import (  # noqa: E402
    build_audit_verdict,
    evaluate_evidence_rules,
    render_markdown_report,
)


OUTPUT_ROOT = REPO_ROOT / "workspace/outputs"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Skill 4: Deterministic verdict engine & evidence audit report CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    rep = subparsers.add_parser("report", help="Run deterministic rule evaluation and render Markdown report.")
    rep.add_argument("--input", required=True, help="JSON bundle containing claim_spec and workflow_state")
    rep.add_argument("--output", default=None, help="Markdown report path relative to workspace/outputs")
    rep.add_argument("--json-verdict", action="store_true", help="Print AuditVerdict JSON instead of Markdown")

    args = parser.parse_args(argv)
    if args.command == "report":
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
        claim_spec = ClaimSpec.model_validate(raw["claim_spec"])
        state = WorkflowState.model_validate(raw["workflow_state"])

        evaluated = evaluate_evidence_rules(claim_spec, state)
        verdict = build_audit_verdict(claim_spec, evaluated)

        if args.json_verdict:
            text = verdict.model_dump_json(indent=2)
        else:
            text = render_markdown_report(claim_spec, verdict, evaluated)

        if args.output:
            root = sandbox_root(OUTPUT_ROOT)
            out_path = resolve_safe_path(root / args.output, [root])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
