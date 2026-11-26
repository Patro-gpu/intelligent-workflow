#!/usr/bin/env python3
"""Skill 2 独立命令行入口（`/verify` 风格）。

示例：
    python -m skills.skill_2_stat_mcp.cli verify \
        --data data/fixtures/case_03_promo_confounding.csv \
        --tool group_compare --target-col col_04 --group-col col_02 --stratify-col col_03
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.skill_2_stat_mcp.engine import StatAuditEngine  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Skill 2: Whitelisted deterministic statistical verifier (/verify).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    v = subparsers.add_parser("verify", help="Run a whitelisted deterministic statistical tool.")
    v.add_argument("--data", required=True, help="CSV path inside allowed read roots")
    v.add_argument(
        "--tool",
        required=True,
        choices=["inspect_data", "aggregate", "group_compare", "sensitivity_check"],
    )
    v.add_argument("--target-col", default=None, help="Safe col_id for target column (e.g. col_04)")
    v.add_argument("--group-col", default=None, help="Safe col_id for group column (e.g. col_02)")
    v.add_argument("--stratify-col", default=None, help="Optional safe col_id for stratification")
    v.add_argument("--metric", default="mean", help="Metric enum")
    v.add_argument("--method", default="winsorize_5pct", help="Sensitivity check method enum")
    v.add_argument("--run-id", default="cli_verify", help="Run identifier for evidence ledger")

    args = parser.parse_args(argv)
    if args.command == "verify":
        engine = StatAuditEngine()
        profile = engine.register_dataset(args.data)
        ds_id = profile["dataset_id"]

        if args.tool == "inspect_data":
            rec = engine.inspect_data(ds_id, run_id=args.run_id)
        elif args.tool == "aggregate":
            rec = engine.aggregate(ds_id, args.target_col, args.metric, run_id=args.run_id)
        elif args.tool == "group_compare":
            rec = engine.group_compare(
                ds_id,
                args.target_col,
                args.group_col,
                metric=args.metric,
                stratify_col_id=args.stratify_col,
                run_id=args.run_id,
            )
        elif args.tool == "sensitivity_check":
            rec = engine.sensitivity_check(
                ds_id,
                args.target_col,
                args.group_col,
                method=args.method,
                run_id=args.run_id,
            )
        else:
            return 1

        print(rec.model_dump_json(indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
