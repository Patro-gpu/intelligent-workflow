"""OpenAI 兼容多模型路由底座（支持 MOCK_MODE、Qwen 诚实降级与硬预算熔断）。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from threading import Lock
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import Field
import yaml

from core.schema import StrictModel
from core.security import resolve_safe_path, sandbox_root

REPO_ROOT = Path(__file__).resolve().parent.parent


class BudgetExceededError(RuntimeError):
    """预算硬上限熔断异常。"""


class ModelCallError(RuntimeError):
    """A provider failed or returned an unusable payload; messages omit provider content."""


class LLMResponse(StrictModel):
    role: str
    requested_provider: str
    used_provider: str
    model: str
    content: str
    parsed_json: Optional[Dict[str, Any]] = None
    used_fallback: bool = False
    mock_mode: bool = True
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cost_cny: float = Field(default=0.0, ge=0.0)
    latency_ms: float = Field(default=0.0, ge=0.0)


_PROVIDER_ENV_PREFIX: Dict[str, str] = {
    "deepseek": "DEEPSEEK",
    "doubao": "DOUBAO",
    "qwen": "QWEN",
    "premium": "PREMIUM",
}

_DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "premium": "https://api.openai.com/v1",
}


_DEFAULT_MODELS: Dict[str, str] = {
    "deepseek": "deepseek-chat",
    "doubao": "doubao-1-5-pro-32k-250115",
    "qwen": "qwen-plus",
    "premium": "gpt-4o",
}


class LLMRouter:
    """统一多模型路由器：支持离线 Mock、超时重试、Qwen 降级、预算硬熔断与脱敏审计日志。"""

    def __init__(
        self,
        config_path: str | Path = REPO_ROOT / "configs/models.yaml",
        log_dir: str | Path = REPO_ROOT / "workspace/outputs/logs",
        mock_mode: Optional[bool] = None,
        max_budget_cny: Optional[float] = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
        simulate_primary_failure_roles: Optional[Set[str]] = None,
    ) -> None:
        load_dotenv(REPO_ROOT / ".env", override=False)

        self.config_path = Path(config_path)
        self.config: Dict[str, Any] = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.roles_cfg: Dict[str, Any] = self.config.get("roles", {})
        self.pricing_cfg: Dict[str, Dict[str, float]] = self.config.get("pricing_cny_per_1k_tokens", {})
        workflow_cfg = self.config.get("workflow", {})

        if mock_mode is None:
            env_mock = os.getenv("MOCK_MODE", "true").strip().lower()
            self.mock_mode = env_mock not in ("0", "false", "no")
        else:
            self.mock_mode = mock_mode

        if max_budget_cny is None:
            env_budget = os.getenv("MAX_TASK_BUDGET_CNY")
            if env_budget is not None:
                self.max_budget_cny = float(env_budget)
            else:
                self.max_budget_cny = float(workflow_cfg.get("max_task_budget_cny", 1.0))
        else:
            self.max_budget_cny = float(max_budget_cny)

        if not math.isfinite(self.max_budget_cny) or self.max_budget_cny < 0:
            raise ValueError("Budget must be finite and non-negative")
        if any(not math.isfinite(float(rate)) or float(rate) < 0
               for rates in self.pricing_cfg.values() for rate in rates.values()):
            raise ValueError("Pricing must be finite and non-negative")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self._lock = Lock()
        self._reserved_cny = 0.0
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.log_dir = sandbox_root(log_dir)
        self.simulate_primary_failure_roles: Set[str] = set(simulate_primary_failure_roles or set())

        self.spent_cny: float = 0.0
        self.call_count: int = 0
        self.role_providers_used: Dict[str, str] = {}

    @property
    def remaining_budget_cny(self) -> float:
        return max(0.0, self.max_budget_cny - self.spent_cny - self._reserved_cny)

    @property
    def model_diversity_lost(self) -> bool:
        """若初审与反方实际落到同一 provider（如同时降级为 qwen），返回 True。"""
        primary = self.role_providers_used.get("primary_reviewer")
        challenger = self.role_providers_used.get("challenger")
        return bool(primary and challenger and primary == challenger)

    def estimate_cost_cny(
        self,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        rates = self.pricing_cfg.get(provider, {"input": 0.002, "output": 0.008})
        cost = (prompt_tokens / 1000.0) * float(rates.get("input", 0.002)) + (
            completion_tokens / 1000.0
        ) * float(rates.get("output", 0.008))
        return round(cost, 8)

    def estimate_reserved_cost_cny(
        self,
        provider: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 512,
    ) -> float:
        """保守预估本次请求成本用于调用前预算熔断。"""
        # Byte-token upper estimate for the supplied messages, including chat framing.
        est_prompt_tokens = 64 + sum(
            32 + sum(len(str(v).encode("utf-8")) for v in m.values()) for m in messages
        )
        return self.estimate_cost_cny(provider, est_prompt_tokens, max_tokens)

    def _resolve_role_providers(self, role: str, provider_override: Optional[str] = None) -> Tuple[str, Optional[str]]:
        if provider_override:
            return provider_override, None
        role_entry = self.roles_cfg.get(role)
        if not role_entry or "provider" not in role_entry:
            raise ValueError(f"Role '{role}' is not configured with an LLM provider in models.yaml")
        return str(role_entry["provider"]), role_entry.get("fallback")

    def _get_provider_credentials(self, provider: str) -> Tuple[str, str, str]:
        prefix = _PROVIDER_ENV_PREFIX.get(provider)
        if not prefix:
            raise ValueError(f"Unsupported provider: '{provider}'")
        api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
        base_url = os.getenv(f"{prefix}_BASE_URL", "").strip()
        model = os.getenv(f"{prefix}_MODEL", _DEFAULT_MODELS[provider]).strip()
        return api_key, base_url or _DEFAULT_BASE_URLS[provider], model

    def _check_budget_or_raise(self, reserved_cost: float, role: str, provider: str) -> None:
        if not math.isfinite(self.max_budget_cny) or reserved_cost > self.remaining_budget_cny + 1e-12:
            self._append_audit_log(
                role=role,
                requested_provider=provider,
                used_provider=provider,
                model="",
                used_fallback=False,
                prompt_tokens=0,
                completion_tokens=0,
                cost_cny=0.0,
                latency_ms=0.0,
                status="budget_exceeded",
            )
            raise BudgetExceededError(
                f"Budget exceeded for role='{role}' provider='{provider}': "
                f"remaining={self.remaining_budget_cny:.6f}, request_reservation={reserved_cost:.6f}"
            )

    def check_live_configuration(self, roles: List[str]) -> None:
        for role in roles:
            providers = self._resolve_role_providers(role)
            if not any(self._has_credentials(p) for p in providers if p):
                raise ModelCallError(f"No configured API key for role '{role}' (check .env)")

    def _has_credentials(self, provider: str) -> bool:
        key, _, _ = self._get_provider_credentials(provider)
        return bool(key and not key.lower().startswith("your_"))

    def _mock_completion(
        self,
        role: str,
        provider: str,
        messages: List[Dict[str, str]],
        expect_json: bool,
        mock_payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Optional[Dict[str, Any]], int, int]:
        if mock_payload is not None:
            payload = mock_payload
        else:
            payload = {
                "status": "ok",
                "role": role,
                "provider": provider,
                "mock": True,
                "message": f"Mock response from {provider} for {role}",
            }
        content = json.dumps(payload, ensure_ascii=False) if expect_json else str(payload["message"])
        prompt_tokens = max(32, sum(len(m.get("content", "")) for m in messages) // 4 + 16)
        completion_tokens = max(16, len(content) // 4 + 8)
        return content, (payload if expect_json else None), prompt_tokens, completion_tokens

    def _live_completion(
        self,
        provider: str,
        messages: List[Dict[str, str]],
        expect_json: bool,
        max_tokens: int,
        temperature: float,
    ) -> Tuple[str, Optional[Dict[str, Any]], Optional[int], Optional[int], str]:
        api_key, base_url, model = self._get_provider_credentials(provider)
        kwargs: Dict[str, Any] = {
            "model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
        }
        if expect_json:
            kwargs["response_format"] = {"type": "json_object"}
        # Every wire attempt must pass our own budget reservation.
        with OpenAI(api_key=api_key, base_url=base_url, timeout=self.timeout_seconds, max_retries=0) as client:
            resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        parsed = json.loads(content) if expect_json else None
        if expect_json and not isinstance(parsed, dict):
            raise ValueError("Expected one JSON object")
        # Missing usage means the request may have consumed the full reservation.
        usage = resp.usage
        p_tok = getattr(usage, "prompt_tokens", None)
        c_tok = getattr(usage, "completion_tokens", None)
        return content, parsed, p_tok, c_tok, model

    def _append_audit_log(
        self,
        *,
        role: str,
        requested_provider: str,
        used_provider: str,
        model: str,
        used_fallback: bool,
        prompt_tokens: int,
        completion_tokens: int,
        cost_cny: float,
        latency_ms: float,
        status: str,
    ) -> None:
        """向 `workspace/outputs/logs/audit_YYYYMMDD.jsonl` 写入脱敏审计日志（严禁密钥或原始数据行）。"""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = resolve_safe_path(self.log_dir / f"audit_{today}.jsonl", [self.log_dir])
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "requested_provider": requested_provider,
            "used_provider": used_provider,
            "model": model,
            "used_fallback": used_fallback,
            "mock_mode": self.mock_mode,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_cny": cost_cny,
            "spent_cny_total": round(self.spent_cny, 8),
            "latency_ms": round(latency_ms, 2),
            "status": status,
        }
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def complete(
        self,
        role: str,
        messages: List[Dict[str, str]],
        *,
        expect_json: bool = True,
        max_tokens: int = 512,
        temperature: float = 0.0,
        provider_override: Optional[str] = None,
        mock_payload: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        """按角色执行模型调用：预算预检 -> 主力调用 -> 失败自动降级 Qwen -> 脱敏审计日志。"""
        primary_provider, fallback_provider = self._resolve_role_providers(role, provider_override)
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")

        providers = [primary_provider] if role == "final_reviewer" else [primary_provider, fallback_provider]
        for provider in dict.fromkeys(providers):
            if not provider or (not self.mock_mode and not self._has_credentials(provider)):
                continue
            attempts = 1 if self.mock_mode or role == "final_reviewer" else 1 + self.max_retries
            for _ in range(attempts):
                reserved = self.estimate_reserved_cost_cny(provider, messages, max_tokens)
                with self._lock:
                    self._check_budget_or_raise(reserved, role, provider)
                    self._reserved_cny += reserved

                started = time.perf_counter()
                cost = reserved
                status = "failed_reserved"
                p_tok = c_tok = None
                model = self._get_provider_credentials(provider)[2]
                try:
                    if self.mock_mode:
                        if provider == primary_provider and role in self.simulate_primary_failure_roles:
                            cost = 0.0
                            raise ModelCallError("Simulated failure")
                        content, parsed, p_tok, c_tok = self._mock_completion(
                            role, provider, messages, expect_json, mock_payload
                        )
                        c_tok = min(c_tok, max_tokens)
                    else:
                        content, parsed, p_tok, c_tok, model = self._live_completion(
                            provider, messages, expect_json, max_tokens, temperature
                        )
                    if p_tok is not None and c_tok is not None and min(p_tok, c_tok) >= 0:
                        cost = self.estimate_cost_cny(provider, p_tok, c_tok)
                        status = "ok"
                    else:
                        status = "ok_reserved"
                except Exception:
                    # Never expose raw provider errors, which may include URLs, keys or prompt fragments.
                    continue
                finally:
                    latency = (time.perf_counter() - started) * 1000
                    with self._lock:
                        self._reserved_cny = round(self._reserved_cny - reserved, 8)
                        self.spent_cny = round(self.spent_cny + cost, 8)
                        self.call_count += 1
                        if status.startswith("ok"):
                            self.role_providers_used[role] = provider
                        self._append_audit_log(
                            role=role, requested_provider=primary_provider, used_provider=provider,
                            model=model, used_fallback=provider != primary_provider,
                            prompt_tokens=p_tok or 0, completion_tokens=c_tok or 0, cost_cny=cost,
                            latency_ms=latency, status=status,
                        )
                return LLMResponse(
                    role=role, requested_provider=primary_provider, used_provider=provider, model=model,
                    content=content, parsed_json=parsed, used_fallback=provider != primary_provider,
                    mock_mode=self.mock_mode, prompt_tokens=p_tok or 0, completion_tokens=c_tok or 0,
                    cost_cny=cost, latency_ms=round(latency, 2),
                )

        raise ModelCallError(f"No usable model response for role '{role}' (check provider settings and audit log)")
