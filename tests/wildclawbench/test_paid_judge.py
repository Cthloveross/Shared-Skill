"""External judging must preserve execution routing and enforce durable accounting."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import gateway
import httpx
from fastapi.testclient import TestClient
from scripts.wildclawbench.paid_judge import BudgetExceeded, CostLedger, judge_payload
from scripts.wildclawbench.protocol import load_config


class Backend:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, **kwargs):
        self.calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 400, "completion_tokens": 100},
            },
        )

    async def aclose(self):
        pass


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.config = {
            **load_config(),
            "runtime_dir": str(self.directory),
            "external_judge": True,
            "judge_base_url": "https://api.deepseek.com/v1",
            "judge_model_id": "deepseek-flash",
            "credential_env_var": "SHAREDSKILL_TEST_CREDENTIAL",
            "paid_api_budget_usd": 10,
        }
        Backend.calls = []

    def tearDown(self):
        self.temporary.cleanup()

    def test_explicit_judge_route_and_private_credential(self):
        with patch.dict("os.environ", {"SHAREDSKILL_TEST_CREDENTIAL": "test-private-secret"}):
            app = gateway.create_app(self.config)
        with TestClient(app) as client, patch.object(gateway.httpx, "AsyncClient", Backend):
            for route, port in [
                ("/runs/t/v1/chat/completions?judge=true", 18181),
                ("/runs/t/judge/v1/chat/completions", None),
            ]:
                response = client.post(route, json={"messages": [], "model": "openai/gpt-5.4", "top_k": 20})
                self.assertEqual(response.status_code, 200)
                request = Backend.calls[-1]
                self.assertEqual(request.url.port, port)
            self.assertEqual(request.url.host, "api.deepseek.com")
            self.assertEqual(request.headers["Authorization"], "Bearer test-private-secret")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "deepseek-flash")
            self.assertNotIn("top_k", payload)
            self.assertEqual(payload["reasoning_effort"], "high")
        for file in self.directory.rglob("*"):
            if file.is_file():
                self.assertNotIn(b"test-private-secret", file.read_bytes())
        state = json.loads((self.directory / "paid-api/budget.json").read_text())
        self.assertEqual(state["requests_settled"], 1)
        self.assertAlmostEqual(state["cost_upper_bound_usd"], 0.0003024)

    def test_judge_audio_remains_local_and_unbilled(self):
        with patch.dict("os.environ", {"SHAREDSKILL_TEST_CREDENTIAL": "secret"}):
            app = gateway.create_app(self.config)
        with TestClient(app) as client, patch.object(gateway.httpx, "AsyncClient", Backend):
            response = client.post(
                "/runs/t/judge/v1/chat/completions",
                json={
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_audio",
                                    "input_audio": {
                                        "data": "UklGRiYAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQIAAAABAA==",
                                        "format": "wav",
                                    },
                                }
                            ],
                        }
                    ]
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Backend.calls[-1].url.port, 18183)
        self.assertFalse((self.directory / "paid-api/budget.json").exists())

    def test_concurrent_reservations_cannot_exceed_cap(self):
        self.config["paid_api_budget_usd"] = 1
        ledger = CostLedger(self.config)

        def reserve(index):
            try:
                return ledger.reserve(str(index))
            except BudgetExceeded:
                return 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            amounts = list(pool.map(reserve, range(10)))
        self.assertEqual(sum(x > 0 for x in amounts), 2)
        self.assertLessEqual(sum(amounts), 1)
        resumed = CostLedger(self.config)
        with self.assertRaises(BudgetExceeded):
            resumed.reserve("new")
        resumed.settle("0", None)
        with self.assertRaises(BudgetExceeded):
            resumed.reserve("later")

    def test_missing_usage_is_not_zero_cost(self):
        ledger = CostLedger(self.config)
        reserved = ledger.reserve("a")
        settled = ledger.settle("a", {})
        self.assertEqual(settled["cost_upper_bound_usd"], reserved)
        self.assertFalse(settled["usage_available"])

    def test_execution_tools_not_accepted_as_judge(self):
        with self.assertRaises(ValueError):
            judge_payload({"messages": [], "tools": [{"type": "function"}]}, self.config)


if __name__ == "__main__":
    unittest.main()
