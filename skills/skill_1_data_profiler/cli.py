#!/usr/bin/env python3
"""Skill 1 独立命令行入口（`/inspect` 风格）。

示例：
    python -m skills.skill_1_data_profiler.cli inspect \
        --data data/fixtures/case_03_promo_confounding.csv
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.skill_1_data_profiler.skill import inspect_csv  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Skill 1: Safe read-only CSV data profiler (/inspect).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_p = subparsers.add_parser("inspect", help="Inspect a CSV inside sandbox allowed roots.")
    inspect_p.add_argument("--data", required=True, help="Path to CSV file inside allowed read roots")
    inspect_p.add_argument("--dataset-id", default=None, help="Optional explicit dataset_id")

    args = parser.parse_args(argv)
    if args.command == "inspect":
        result = inspect_csv(args.data, dataset_id=args.dataset_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
