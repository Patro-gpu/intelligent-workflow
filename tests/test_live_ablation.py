"""Exercise the live code paths offline; never contact a paid provider in pytest."""

import csv
import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

from core.llm_router import BudgetExceededError, LLMRouter
from evals import run_ablation as evaluation


def configure_test_keys(monkeypatch):
    for provider in ("DEEPSEEK", "DOUBAO", "QWEN"):
        monkeypatch.setenv(f"{provider}_API_KEY", "test_key")
    monkeypatch.setenv("MOCK_MODE", "true")
    monkeypatch.setenv("MAX_TASK_BUDGET_CNY", "1.0")


def test_live_cap_accounts_for_failed_attempt_and_blocks_fallback(tmp_path, monkeypatch):
    configure_test_keys(monkeypatch)
    router = LLMRouter(mock_mode=False, max_retries=0, log_dir=tmp_path)
    messages = [{"role": "user", "content": "json"}]
    reservation = router.estimate_reserved_cost_cny("deepseek", messages)
    router.max_budget_cny = reservation
    attempts = []

    def fail(provider, *args):
        attempts.append(provider)
        raise RuntimeError("provider private diagnostics")

    monkeypatch.setattr(router, "_live_completion", fail)
    with pytest.raises(BudgetExceededError):
        router.complete("primary_reviewer", messages)
    assert attempts == ["deepseek"]
    assert router.spent_cny == reservation
    log = next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8")
    assert "failed_reserved" in log and "provider private diagnostics" not in log


def test_live_in_flight_budget_cannot_be_spent_twice(tmp_path, monkeypatch):
    configure_test_keys(monkeypatch)
    router = LLMRouter(mock_mode=False, log_dir=tmp_path)
    messages = [{"role": "user", "content": "json"}]
    router.max_budget_cny = router.estimate_reserved_cost_cny("deepseek", messages)
    entered, release = Event(), Event()

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return "{}", {}, None, None, "model"

    monkeypatch.setattr(router, "_live_completion", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(router.complete, "primary_reviewer", messages)
        try:
            assert entered.wait(5)
            with pytest.raises(BudgetExceededError):
                router.complete("challenger", messages)
        finally:
            release.set()
        assert pending.result().cost_cny == router.max_budget_cny
    assert router.remaining_budget_cny == 0


def test_live_ablation_calls_real_provider_adapter_and_labels_report(tmp_path, monkeypatch):
    configure_test_keys(monkeypatch)
    calls = []
    case = json.loads((evaluation.FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))["cases"][0]
    real_read = evaluation.Path.read_text

    def read_one_case(path, *args, **kwargs):
        if path == evaluation.FIXTURES_DIR / "manifest.json":
            return json.dumps({"cases": [case]})
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(evaluation.Path, "read_text", read_one_case)

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            user = json.loads(body["messages"][-1]["content"])
            payload = {"predicted": "limited"}
            if "ClaimSpec" in user:
                payload = {"claim_id": user["ClaimSpec"]["claim_id"], "position": "uncertain", "issues": []}
            wire = json.dumps({
                "id": "local-test", "object": "chat.completion", "created": 1, "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(payload)},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 16, "completion_tokens": 12, "total_tokens": 28},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for provider in ("DEEPSEEK", "DOUBAO", "QWEN"):
                monkeypatch.setenv(f"{provider}_MODEL", provider)
                monkeypatch.setenv(f"{provider}_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
            result = evaluation.run_ablation(scale="mvp", live=True, output_dir=tmp_path)
        finally:
            server.shutdown()
            thread.join()

    for path, body in calls:
        assert path == "/v1/chat/completions"
        assert body["response_format"] == {"type": "json_object"}
        assert body["max_tokens"] > 0
        user = json.loads(body["messages"][-1]["content"])
        assert not {"allowed_verdicts", "generation_truth", "observable_evidence"}.intersection(user)
    assert sorted(body["model"] for _, body in calls) == ["DEEPSEEK", "DEEPSEEK", "DEEPSEEK", "DOUBAO"]
    report = evaluation.Path(result["output_dir"]) / "ablation_results.csv"
    with report.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 4 and all(r["mode"] == "live" for r in rows)
    assert rows[1]["predicted"] == "limited" and int(rows[1]["tool_calls"]) == 0
    assert float(rows[2]["cost_cny"]) == pytest.approx((16 * 0.002 + 12 * 0.008) / 1000)
    assert float(rows[3]["cost_cny"]) == pytest.approx(float(rows[2]["cost_cny"]) * 1.5)


@pytest.mark.parametrize("limit", [float("nan"), float("inf"), -1])
def test_invalid_budget_rejected_before_calls(limit, tmp_path):
    with pytest.raises(ValueError, match="finite"):
        LLMRouter(mock_mode=False, max_budget_cny=limit, log_dir=tmp_path)


def test_live_budget_stop_retains_only_completed_rows(tmp_path, monkeypatch):
    configure_test_keys(monkeypatch)
    with pytest.raises(BudgetExceededError, match="Completed rows"):
        evaluation.run_ablation(scale="mvp", live=True, budget_cny=0, output_dir=tmp_path)
    partial = next(tmp_path.glob("live/*/ablation_results.partial.csv"))
    with partial.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["system"] for r in rows] == ["R0_fixed_script"]
    assert not (partial.parent / "ablation_report.md").exists()
    assert not (partial.parent / "ablation_results.csv").exists()
