"""OpenAI 兼容多模型路由底座（支持 MOCK_MODE、Qwen 诚实降级与硬预算熔断）。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import Field
import yaml

from core.schema import StrictModel

REPO_ROOT = Path(__file__).resolve().parent.parent


class BudgetExceededError(RuntimeError):
    """预算硬上限熔断异常。"""


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
        log_dir: str | Path = REPO_ROOT / "workspace/logs",
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

        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.log_dir = Path(log_dir)
        self.simulate_primary_failure_roles: Set[str] = set(simulate_primary_failure_roles or set())

        self.spent_cny: float = 0.0
        self.call_count: int = 0
        self.role_providers_used: Dict[str, str] = {}

    @property
    def remaining_budget_cny(self) -> float:
        return max(0.0, self.max_budget_cny - self.spent_cny)

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
        approx_chars = sum(len(m.get("content", "")) for m in messages)
        est_prompt_tokens = max(64, approx_chars // 3 + 32)
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
        return api_key, base_url, model

    def _check_budget_or_raise(self, reserved_cost: float, role: str, provider: str) -> None:
        if self.spent_cny + reserved_cost > self.max_budget_cny + 1e-12:
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
                f"spent={self.spent_cny:.6f} + reserved={reserved_cost:.6f} > max={self.max_budget_cny:.6f}"
            )

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
    ) -> Tuple[str, Optional[Dict[str, Any]], int, int, str]:
        api_key, base_url, model = self._get_provider_credentials(provider)
        if not api_key or api_key.lower().startswith("your_"):
            raise RuntimeError(f"Missing real API key for provider '{provider}'")

        client = OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=self.timeout_seconds,
        )
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if expect_json:
            kwargs["response_format"] = {"type": "json_object"}

        last_exc: Optional[Exception] = None
        for _ in range(self.max_retries + 1):
            try:
                resp = client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or ""
                usage = getattr(resp, "usage", None)
                prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                parsed_json = json.loads(content) if expect_json else None
                return content, parsed_json, prompt_tokens, completion_tokens, model
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        raise RuntimeError(f"Live call failed for provider '{provider}': {last_exc}") from last_exc

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
        """向 `workspace/logs/audit_YYYYMMDD.jsonl` 写入脱敏审计日志（严禁密钥或原始数据行）。"""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = self.log_dir / f"audit_{today}.jsonl"
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
        reserved_cost = self.estimate_reserved_cost_cny(primary_provider, messages, max_tokens=max_tokens)
        self._check_budget_or_raise(reserved_cost, role=role, provider=primary_provider)

        start_ts = time.perf_counter()
        used_provider = primary_provider
        used_fallback = False

        try:
            if self.mock_mode:
                if role in self.simulate_primary_failure_roles:
                    raise RuntimeError(f"Simulated primary failure for role='{role}'")
                _, _, model = self._get_provider_credentials(primary_provider)
                content, parsed_json, p_tok, c_tok = self._mock_completion(
                    role=role,
                    provider=primary_provider,
                    messages=messages,
                    expect_json=expect_json,
                    mock_payload=mock_payload,
                )
            else:
                content, parsed_json, p_tok, c_tok, model = self._live_completion(
                    provider=primary_provider,
                    messages=messages,
                    expect_json=expect_json,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
        except Exception:
            if not fallback_provider:
                raise
            used_provider = fallback_provider
            used_fallback = True
            fallback_reserved = self.estimate_reserved_cost_cny(
                fallback_provider, messages, max_tokens=max_tokens
            )
            self._check_budget_or_raise(fallback_reserved, role=role, provider=fallback_provider)

            if self.mock_mode:
                _, _, model = self._get_provider_credentials(fallback_provider)
                content, parsed_json, p_tok, c_tok = self._mock_completion(
                    role=role,
                    provider=fallback_provider,
                    messages=messages,
                    expect_json=expect_json,
                    mock_payload=mock_payload,
                )
            else:
                content, parsed_json, p_tok, c_tok, model = self._live_completion(
                    provider=fallback_provider,
                    messages=messages,
                    expect_json=expect_json,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )

        latency_ms = (time.perf_counter() - start_ts) * 1000.0
        cost_cny = self.estimate_cost_cny(used_provider, p_tok, c_tok)
        self.spent_cny = round(self.spent_cny + cost_cny, 8)
        self.call_count += 1
        self.role_providers_used[role] = used_provider

        self._append_audit_log(
            role=role,
            requested_provider=primary_provider,
            used_provider=used_provider,
            model=model,
            used_fallback=used_fallback,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            cost_cny=cost_cny,
            latency_ms=latency_ms,
            status="ok",
        )

        return LLMResponse(
            role=role,
            requested_provider=primary_provider,
            used_provider=used_provider,
            model=model,
            content=content,
            parsed_json=parsed_json,
            used_fallback=used_fallback,
            mock_mode=self.mock_mode,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            cost_cny=cost_cny,
            latency_ms=round(latency_ms, 2),
        )
