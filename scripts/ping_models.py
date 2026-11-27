#!/usr/bin/env python3
"""DeepSeek / 豆包 / Qwen / 昂贵终裁模型连通性与 JSON 稳定性实测脚本。

默认遵循环境变量 `MOCK_MODE`（默认为 `true`）。
在配置好 `.env` 后可通过：
    MOCK_MODE=false python scripts/ping_models.py
实测真实 API 连通性与 JSON 输出稳定性。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.llm_router import LLMRouter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Ping configured LLM providers for connectivity and JSON stability.")
    parser.add_argument("--include-qwen", action="store_true", help="Also ping Qwen fallback provider")
    parser.add_argument("--include-premium", action="store_true", help="Also ping premium final judge provider")
    args = parser.parse_args()

    router = LLMRouter()
    targets: list[tuple[str, str]] = [
        ("primary_reviewer", "deepseek"),
        ("challenger", "doubao"),
    ]
    if args.include_qwen:
        targets.append(("fallback_check", "qwen"))
    if args.include_premium:
        targets.append(("final_reviewer", "premium"))

    mode_label = "MOCK" if router.mock_mode else "LIVE"
    print(f"=== Shuzheng Audit Model Ping ({mode_label} mode, budget={router.max_budget_cny} CNY) ===")

    all_ok = True
    for role, provider in targets:
        try:
            text_resp = router.complete(
                role=role,
                provider_override=provider,
                messages=[{"role": "user", "content": "Reply with one word: pong"}],
                expect_json=False,
                max_tokens=32,
            )
            json_resp = router.complete(
                role=role,
                provider_override=provider,
                messages=[
                    {
                        "role": "user",
                        "content": 'Return a valid JSON object with keys "status" and "provider".',
                    }
                ],
                expect_json=True,
                max_tokens=64,
            )
            print(
                f"[PASS] provider={provider:<8} model={json_resp.model:<26} "
                f"text_ok={bool(text_resp.content)} json_ok={isinstance(json_resp.parsed_json, dict)} "
                f"cost={text_resp.cost_cny + json_resp.cost_cny:.6f} CNY"
            )
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"[FAIL] provider={provider:<8} error={exc}", file=sys.stderr)

    print(f"Total spent: {router.spent_cny:.6f} CNY | Remaining: {router.remaining_budget_cny:.6f} CNY")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
