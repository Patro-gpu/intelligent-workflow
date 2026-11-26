#!/usr/bin/env python3
"""Skill 3 独立命令行入口（`/challenge` 风格）。

示例：
    python -m skills.skill_3_claim_planner.cli challenge \
        --data data/fixtures/case_03_promo_confounding.csv \
        --claim "发放消费券使商户订单增加了 48 单。" \
        --context "数据包含同期大促参与标记 is_promo"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.llm_router import LLMRouter  # noqa: E402
from core.security import DatasetRegistry  # noqa: E402
from skills.skill_3_claim_planner.skill import (  # noqa: E402
    deduplicate_and_plan_tasks,
    freeze_claim,
    run_blind_review,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Skill 3: Claim freezing & blind review verification planner.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    challenge_p = subparsers.add_parser("challenge", help="Freeze a claim and generate Round-1 blind review plan.")
    challenge_p.add_argument("--data", required=True, help="Path to CSV inside allowed roots")
    challenge_p.add_argument("--claim", required=True, help="Natural-language claim to audit")
    challenge_p.add_argument("--claim-id", default="claim_cli", help="Claim identifier")
    challenge_p.add_argument("--context", default="", help="Optional context notes")
    challenge_p.add_argument("--mock", action="store_true", default=True, help="Use offline MOCK_MODE (default: True)")
    challenge_p.add_argument("--live", action="store_true", help="Use live LLM APIs instead of mock mode")

    args = parser.parse_args(argv)
    if args.command == "challenge":
        data_path = Path(args.data).expanduser().resolve()
        registry = DatasetRegistry(
            allowed_roots=[
                REPO_ROOT / "data/fixtures",
                REPO_ROOT / "workspace/inputs",
                data_path.parent,
            ]
        )
        profile = registry.register_csv(data_path)
        router = LLMRouter(mock_mode=not args.live)

        claim_spec = freeze_claim(
            args.claim,
            claim_id=args.claim_id,
            context_notes=args.context,
            router=router,
        )
        primary_out, challenger_out, diversity_lost = run_blind_review(
            claim_spec, profile, router=router
        )
        issues, tasks, skipped = deduplicate_and_plan_tasks(
            [("primary_reviewer", primary_out), ("challenger", challenger_out)],
            dataset_hash=profile.dataset_hash,
        )
        output = {
            "claim_spec": claim_spec.model_dump(mode="json"),
            "dataset_id": profile.dataset_id,
            "model_diversity_lost": diversity_lost,
            "issue_count": len(issues),
            "planned_task_count": len(tasks),
            "skipped_duplicate_tasks": skipped,
            "planned_tasks": [t.model_dump(mode="json") for t in tasks],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
