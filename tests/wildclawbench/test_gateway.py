"""Routing tests prove model aliases and image/audio requests remain local."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import gateway
import httpx
from fastapi.testclient import TestClient
from scripts.wildclawbench.protocol import load_config


class LocalBackend:
    calls = []

    def __init__(self, *args, **kwargs):
        self.asserted_trust_env = kwargs.get("trust_env")

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, **kwargs):
        self.calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def aclose(self):
        pass


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = {**load_config(), "runtime_dir": self.directory.name}
        self.app = gateway.create_app(self.config)
        LocalBackend.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def request(self, endpoint, body):
        with TestClient(self.app) as client, patch.object(gateway.httpx, "AsyncClient", LocalBackend):
            response = client.post("/runs/test-run/v1/" + endpoint, json=body)
        self.assertEqual(response.status_code, 200)
        return LocalBackend.calls[-1]

    def test_remote_alias_resolves_local_with_frozen_sampling(self):
        request = self.request(
            "chat/completions",
            {
                "model": "openai/gpt-5.4",
                "temperature": 0.7,
                "messages": [{"role": "user", "content": "task"}],
                "max_tokens": 100000,
            },
        )
        self.assertEqual(request.url.host, "127.0.0.1")
        self.assertEqual(request.url.port, 18181)
        body = json.loads(request.content)
        self.assertEqual(body["model"], "qwen3.8-27b")
        self.assertEqual(body["temperature"], 1)
        self.assertEqual(body["seed"], 42)
        self.assertEqual(body["max_tokens"], 16384)
        evidence = list((Path(self.directory.name) / "api-evidence/test-run").glob("*/record.json"))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(json.loads(evidence[0].read_text())["status"], "completed")

    def test_audio_input_uses_audio_service(self):
        request = self.request(
            "chat/completions",
            {
                "model": "openai/gpt-4o-mini-audio-preview",
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
                ],
            },
        )
        self.assertEqual(request.url.port, 18183)
        self.assertEqual(json.loads(request.content)["model"], "local-audio")

    def test_legacy_judge_limit_does_not_consume_thinking_answer_budget(self):
        request = self.request(
            "chat/completions", {"model": "openai/gpt-5.4", "messages": [], "max_completion_tokens": 512}
        )
        body = json.loads(request.content)
        self.assertEqual(body["max_tokens"], 16384)
        self.assertNotIn("max_completion_tokens", body)

    def test_tool_schema_type_is_not_audio_and_preserves_incoming_request(self):
        body = {
            "model": "qwen3.8-27b",
            "messages": [{"role": "user", "content": "Use the tool"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "probe",
                        "parameters": {"type": "object", "properties": {"type": {"type": "string"}}},
                    },
                }
            ],
        }
        request = self.request("chat/completions", body)
        self.assertEqual(request.url.port, 18181)
        incoming = next((Path(self.directory.name) / "api-evidence/test-run").glob("*/incoming.json"))
        self.assertEqual(json.loads(incoming.read_text()), body)

    def test_invalid_body_is_recorded(self):
        with TestClient(self.app) as client:
            response = client.post("/runs/test-run/v1/chat/completions", json=["invalid"])
        self.assertEqual(response.status_code, 400)
        record = next((Path(self.directory.name) / "api-evidence/test-run").glob("*/record.json"))
        self.assertEqual(json.loads(record.read_text())["status"], "error")

    def test_stream_usage_is_requested(self):
        # Failure is sufficient to inspect the exact upstream request without a fake stream.
        class RejectedBackend(LocalBackend):
            async def send(self, request, **kwargs):
                self.calls.append(request)
                return httpx.Response(400, json={"error": "test rejection"})

        with TestClient(self.app) as client, patch.object(gateway.httpx, "AsyncClient", RejectedBackend):
            response = client.post(
                "/runs/test-run/v1/chat/completions", json={"model": "qwen3.8-27b", "messages": [], "stream": True}
            )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(json.loads(LocalBackend.calls[-1].content)["stream_options"]["include_usage"])

    def test_image_output_avoids_nested_loopback_urls(self):
        request = self.request("images/generations", {"model": "any", "prompt": "real generated picture"})
        self.assertEqual(request.url.port, 18184)
        self.assertEqual(json.loads(request.content)["response_format"], "b64_json")

    def test_remote_endpoints_rejected_by_config(self):
        original = Path(self.directory.name) / "config.json"
        invalid = dict(self.config, model_base_url="https://api.openai.com/v1")
        original.write_text(json.dumps(invalid))
        with self.assertRaises(ValueError):
            load_config(original)


if __name__ == "__main__":
    unittest.main()
