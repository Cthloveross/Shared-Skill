"""Bound task-agent isolation, exact context budgets, and media contracts.

All upstream and tokenizer calls are fake; no model or paid provider is used.
"""

import asyncio
import base64
import copy
import io
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import alignment_catalog as catalog
from scripts.wildclawbench import gateway
import httpx
from fastapi.testclient import TestClient
from scripts.wildclawbench.protocol import load_config


class FakeBackend:
    calls = []
    token_count = 32
    token_status = 200
    token_result = None

    def __init__(self, *args, **kwargs):
        if kwargs.get("trust_env") is not False:
            raise AssertionError("Upstreams must ignore proxy environment variables")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.send(self.build_request("POST", url, **kwargs))

    async def send(self, request, **kwargs):
        type(self).calls.append(request)
        if request.url.path.endswith("/tokenize"):
            return httpx.Response(
                type(self).token_status,
                request=request,
                json=type(self).token_result
                if type(self).token_result is not None
                else {"count": type(self).token_count, "max_model_len": 131072},
            )
        return httpx.Response(
            200,
            request=request,
            json={"model": "qwen3.8-27b", "choices": [{"message": {"content": "fake completion"}}]},
        )

    async def aclose(self):
        pass


class AlignmentGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {
            **load_config(),
            "runtime_dir": str(self.root / "runtime"),
            "external_judge": False,
            "protocol_hash": "frozen-protocol",
            "alignment": {"version": "native-night-atomic-v1"},
        }
        self.bank = self.root / "bank"
        skill = self.bank / "alpha"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            '---\nname: alpha\ndescription: "Inspect actual inputs."\n---\nRun the procedure.\n'
        )
        self.binding = catalog.prepare_binding(self.config, "run-a", "execution-a", self.bank)
        self.app = gateway.create_app(self.config)
        FakeBackend.calls = []
        FakeBackend.token_count = 32
        FakeBackend.token_status = 200
        FakeBackend.token_result = None

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, **kwargs):
        with TestClient(self.app) as client, patch.object(gateway.httpx, "AsyncClient", FakeBackend):
            return client.post(path, **kwargs)

    def agent(self, body=None, run="run-a", execution="execution-a"):
        return self.post(
            f"/runs/{run}/executions/{execution}/agent/v1/chat/completions",
            json=body if body is not None else self.body(),
        )

    def body(self):
        return {
            "model": self.config["model_id"],
            "messages": [
                {"role": "system", "content": "Native catalog and instructions."},
                {"role": "user", "content": "Solve the task."},
            ],
            "tools": [{"type": "function", "function": {"name": "inspect", "parameters": {"type": "object"}}}],
        }

    def records(self):
        return [json.loads(p.read_text()) for p in (self.root / "runtime/api-evidence").glob("*/*/record.json")]

    def test_agent_tokenizes_full_catalog_tools_and_effective_template_without_mutating_input(self):
        original = self.body()
        before = copy.deepcopy(original)
        response = self.agent(original)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(original, before)
        self.assertEqual(len(FakeBackend.calls), 2)
        tokenized, forwarded = [json.loads(r.content) for r in FakeBackend.calls]
        self.assertEqual(tokenized["messages"], forwarded["messages"])
        self.assertEqual(tokenized["tools"], original["tools"])
        self.assertEqual(tokenized["chat_template_kwargs"], forwarded["chat_template_kwargs"])
        self.assertTrue(tokenized["add_generation_prompt"])
        self.assertEqual(forwarded["messages"][0]["content"].count(catalog.CATALOG_MARKER), 1)
        self.assertIn("/root/skills/alpha/SKILL.md", forwarded["messages"][0]["content"])
        incoming = next((self.root / "runtime/api-evidence/run-a").glob("*/incoming.json"))
        self.assertEqual(json.loads(incoming.read_text()), original)
        record = self.records()[0]
        self.assertEqual(record["purpose"], "task_agent")
        self.assertEqual(record["execution_id"], "execution-a")
        for field in ["protocol_hash", "bank_sha256", "catalog_sha256"]:
            self.assertEqual(record[field], self.binding[field])
        self.assertEqual(record["requested_model"], record["actual_model"])
        self.assertEqual(record["response_model"], self.config["model_id"])
        self.assertEqual(record["context_budget"]["max_output_tokens"], 16384)
        self.assertEqual(record["context_budget"]["context_limit"], 131072)

    def test_exact_context_boundary_passes_and_one_token_over_fails_without_model_call(self):
        FakeBackend.token_count = 131072 - 16384
        self.assertEqual(self.agent().status_code, 200)
        FakeBackend.calls = []
        FakeBackend.token_count += 1
        response = self.agent()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"], "context_length_exceeded")
        self.assertEqual(len(FakeBackend.calls), 1)
        record = max(self.records(), key=lambda r: r["started_unix"])
        self.assertFalse(record["upstream_attempted"])
        self.assertEqual(record["context_budget"]["reserved_total_tokens"], 131073)

    def test_tokenizer_failure_or_invalid_count_fails_closed(self):
        for status, payload in [(503, None), (200, {"count": True}), (200, {"count": -1}), (200, {})]:
            with self.subTest(status=status, payload=payload):
                FakeBackend.calls = []
                FakeBackend.token_status, FakeBackend.token_result = status, payload
                response = self.agent()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["type"], "context_tokenization_failed")
                self.assertEqual(len(FakeBackend.calls), 1)

    def test_custom_tokenizer_endpoint_is_used(self):
        self.config["agent_tokenizer_url"] = "http://127.0.0.1:19999/custom/tokenize"
        self.app = gateway.create_app(self.config)
        self.assertEqual(self.agent().status_code, 200)
        self.assertEqual(str(FakeBackend.calls[0].url), self.config["agent_tokenizer_url"])

    def test_remote_tokenizer_is_rejected_before_any_network_request(self):
        self.config["agent_tokenizer_url"] = "http://example.invalid/tokenize"
        self.app = gateway.create_app(self.config)
        self.assertEqual(self.agent().status_code, 503)
        self.assertFalse(FakeBackend.calls)

    def test_backend_context_limit_is_respected(self):
        FakeBackend.token_result = {"count": 32, "max_model_len": 16384}
        self.assertEqual(self.agent().status_code, 400)
        self.assertEqual(len(FakeBackend.calls), 1)
        self.assertEqual(self.records()[0]["context_budget"]["context_limit"], 16384)

    def test_mutated_bank_missing_binding_or_wrong_protocol_never_reach_tokenizer(self):
        for scenario in ["missing", "protocol", "bank"]:
            with self.subTest(scenario=scenario):
                FakeBackend.calls = []
                if scenario == "missing":
                    response = self.agent(execution="unknown")
                elif scenario == "scripts.wildclawbench.protocol":
                    self.config["protocol_hash"] = "other-protocol"
                    response = self.agent()
                    self.config["protocol_hash"] = "frozen-protocol"
                else:
                    (self.bank / "alpha/SKILL.md").write_text("changed bytes")
                    response = self.agent()
                self.assertEqual(response.status_code, 409)
                self.assertFalse(FakeBackend.calls)

    def test_separate_executions_use_separate_banks_and_catalogs(self):
        other = self.root / "other"
        (other / "beta").mkdir(parents=True)
        (other / "beta/SKILL.md").write_text('---\nname: beta\ndescription: "Other branch."\n---\nAct.\n')
        second = catalog.prepare_binding(self.config, "run-b", "execution-b", other)
        self.assertEqual(self.agent().status_code, 200)
        self.assertEqual(self.agent(run="run-b", execution="execution-b").status_code, 200)
        forwards = [json.loads(x.content) for x in FakeBackend.calls if x.url.path.endswith("chat/completions")]
        self.assertIn("/alpha/SKILL.md", forwards[0]["messages"][0]["content"])
        self.assertNotIn("/beta/SKILL.md", forwards[0]["messages"][0]["content"])
        self.assertIn("/beta/SKILL.md", forwards[1]["messages"][0]["content"])
        self.assertNotEqual(self.binding["bank_sha256"], second["bank_sha256"])

    def test_unbound_main_tool_or_evolver_counts_context_without_loading_or_injecting_catalog(self):
        with patch.object(gateway, "load_binding", side_effect=AssertionError("Unexpected catalog access")):
            body = self.body()
            response = self.post("/runs/evolver-or-tool/v1/chat/completions", json=body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(FakeBackend.calls), 2)
            self.assertEqual(json.loads(FakeBackend.calls[0].content)["messages"], body["messages"])
            self.assertEqual(json.loads(FakeBackend.calls[1].content)["messages"], body["messages"])
            self.assertEqual(self.records()[0]["purpose"], "unbound_execution")
            self.assertIn("context_budget", self.records()[0])
            self.assertNotIn("catalog_sha256", self.records()[0])
            self.assertEqual(self.post("/runs/grade/judge/v1/chat/completions", json=body).status_code, 404)

    def test_agent_rejects_wrong_model_and_media_without_fallback(self):
        body = self.body()
        body["model"] = "other-model"
        self.assertEqual(self.agent(body).status_code, 400)
        body = self.body()
        body["messages"] = [
            {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "data", "format": "wav"}}]}
        ]
        self.assertEqual(self.agent(body).status_code, 400)
        response = self.post("/runs/run-a/executions/execution-a/agent/v1/audio/speech", json={"input": "text"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(FakeBackend.calls)

    def test_audio_judge_multipart_preserves_original_bytes_and_avoids_paid_route(self):
        response = self.post(
            "/runs/audio-grade/judge/v1/audio/transcriptions",
            files={"file": ("clip.wav", b"RIFF-original-audio", "audio/wav")},
            data={"model": "original-asr-alias", "response_format": "text"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(FakeBackend.calls), 1)
        request = FakeBackend.calls[0]
        self.assertEqual(request.url.port, 18183)
        self.assertIn(b"RIFF-original-audio", request.content)
        self.assertEqual(request.headers["authorization"], "Bearer local-experiment")
        record = self.records()[0]
        self.assertEqual(record["purpose"], "audio_judge")
        self.assertEqual(record["role"], "audio")
        self.assertEqual(record["requested_model"], "original-asr-alias")
        self.assertEqual(record["actual_model"], "local-audio")

    def test_audio_judge_json_stays_omni_and_has_no_catalog(self):
        body = {
            "model": "original-audio-judge",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "input_audio", "input_audio": {"format": "wav", "data": "UklGRg=="}}],
                }
            ],
        }
        response = self.post("/runs/grade/judge/v1/chat/completions", json=body)
        self.assertEqual(response.status_code, 200)
        forwarded = json.loads(FakeBackend.calls[0].content)
        self.assertEqual(forwarded["messages"], body["messages"])
        self.assertEqual(forwarded["model"], "local-audio")
        self.assertEqual(self.records()[0]["purpose"], "audio_judge")

    def audio_body(self, raw):
        return {
            "model": "local-audio",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"format": "wav", "data": base64.b64encode(raw).decode()},
                        }
                    ],
                }
            ],
        }

    def pcm_wav(self, frames):
        stream = io.BytesIO()
        with wave.open(stream, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(frames)
        return stream.getvalue()

    def test_zero_frame_wav_returns_400_without_upstream_and_retains_original_payload(self):
        body = self.audio_body(self.pcm_wav(b""))
        response = self.post("/runs/empty-wav/v1/chat/completions", json=body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("zero frames", response.json()["error"]["message"])
        self.assertFalse(FakeBackend.calls)
        record = self.records()[0]
        self.assertFalse(record["upstream_attempted"])
        incoming = self.root / "runtime/api-evidence/empty-wav" / record["request_id"] / "incoming.json"
        self.assertEqual(json.loads(incoming.read_text()), body)

    def test_nonempty_pcm_wav_is_forwarded_byte_for_byte(self):
        original = self.pcm_wav(b"\x01\x00" * 160)
        body = self.audio_body(original)
        response = self.post("/runs/nonempty-wav/v1/chat/completions", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(FakeBackend.calls), 1)
        forwarded = json.loads(FakeBackend.calls[0].content)
        self.assertEqual(forwarded["messages"], body["messages"])
        self.assertEqual(base64.b64decode(forwarded["messages"][0]["content"][0]["input_audio"]["data"]), original)
        self.assertEqual(FakeBackend.calls[0].url.port, 18183)

    def test_float_or_extensible_wav_not_supported_by_stdlib_is_not_mistaken_for_empty(self):
        for tag, extension in [
            (3, b""),
            (
                0xFFFE,
                struct.pack("<HHI", 22, 32, 0) + b"\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71",
            ),
        ]:
            with self.subTest(format_tag=tag):
                FakeBackend.calls = []
                fmt = struct.pack("<HHIIHH", tag, 1, 16000, 64000, 4, 32) + extension
                chunks = (
                    b"fmt "
                    + struct.pack("<I", len(fmt))
                    + fmt
                    + b"data"
                    + struct.pack("<I", 4)
                    + struct.pack("<f", 0.25)
                )
                original = b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks
                response = self.post("/runs/float-wav/v1/chat/completions", json=self.audio_body(original))
                self.assertEqual(response.status_code, 200)
                data = json.loads(FakeBackend.calls[0].content)["messages"][0]["content"][0]["input_audio"]["data"]
                self.assertEqual(base64.b64decode(data), original)

    def test_invalid_base64_audio_is_400_without_upstream(self):
        body = self.audio_body(b"audio")
        body["messages"][0]["content"][0]["input_audio"]["data"] = "not-base64!"
        response = self.post("/runs/invalid-base64/v1/chat/completions", json=body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("base64", response.json()["error"]["message"])
        self.assertFalse(FakeBackend.calls)

    def test_unsupported_modal_endpoints_and_formats_are_explicit_without_upstream_calls(self):
        for endpoint in ["audio/translations", "images/edits"]:
            self.assertEqual(self.post("/runs/test/v1/" + endpoint, json={}).status_code, 404)
        for part in [
            {"type": "audio_url", "audio_url": {"url": "https://example.invalid/audio.wav"}},
            {"type": "input_audio", "input_audio": {"data": "audio", "format": "unsupported"}},
            {"type": "input_audio", "input_audio": {"data": "audio", "format": []}},
        ]:
            self.assertEqual(
                self.post(
                    "/runs/test/v1/chat/completions", json={"messages": [{"role": "user", "content": [part]}]}
                ).status_code,
                400,
            )
        self.assertEqual(
            self.post("/runs/test/v1/audio/speech", json={"input": "text", "response_format": "srt"}).status_code, 400
        )
        self.assertEqual(
            self.post("/runs/test/v1/audio/speech", json={"input": "text", "response_format": []}).status_code, 400
        )
        self.assertEqual(
            self.post(
                "/runs/test/v1/audio/transcriptions",
                files={"file": ("a.wav", b"audio")},
                data={"response_format": "verbose_json"},
            ).status_code,
            400,
        )
        self.assertFalse(FakeBackend.calls)

    def test_invalid_multipart_does_not_reach_upstream(self):
        for data, content_type in [
            (b"not multipart", "multipart/form-data; boundary=none"),
            (
                b'--x\r\nContent-Disposition: form-data; name="model"\r\n\r\nlocal\r\n--x--\r\n',
                "multipart/form-data; boundary=x",
            ),
        ]:
            self.assertEqual(
                self.post(
                    "/runs/test/judge/v1/audio/transcriptions", content=data, headers={"Content-Type": content_type}
                ).status_code,
                400,
            )
        self.assertFalse(FakeBackend.calls)


class AlignmentPaidQueueTests(unittest.TestCase):
    def exercise(self, operation):
        with tempfile.TemporaryDirectory() as temporary:
            config = {
                **load_config(),
                "runtime_dir": temporary,
                "external_judge": True,
                "alignment": {"version": "native-night-atomic-v1"},
                "judge_base_url": "https://api.deepseek.com/v1",
                "judge_model_id": "deepseek-flash",
                "paid_api_budget_usd": 1,
            }
            with patch.object(gateway, "credential", return_value="test-only-placeholder"):
                app = gateway.create_app(config)
            real_client = httpx.AsyncClient

            async def run():
                class QueuedBackend(FakeBackend):
                    calls = []
                    active = peak = 0
                    started, release = asyncio.Event(), asyncio.Event()

                    async def send(self, request, **kwargs):
                        cls = type(self)
                        cls.calls.append(request)
                        cls.active += 1
                        cls.peak = max(cls.peak, cls.active)
                        if len(cls.calls) >= 2:
                            cls.started.set()
                        try:
                            await cls.release.wait()
                            return httpx.Response(
                                200,
                                request=request,
                                json={
                                    "choices": [{"message": {"content": "real fixture"}}],
                                    "usage": {
                                        "prompt_tokens": 1000,
                                        "prompt_cache_hit_tokens": 400,
                                        "completion_tokens": 100,
                                    },
                                },
                            )
                        finally:
                            cls.active -= 1

                async with real_client(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                    with patch.object(gateway.httpx, "AsyncClient", QueuedBackend):
                        await operation(client, QueuedBackend, Path(temporary) / "paid-api/budget.json")

            asyncio.run(run())

    def test_three_paid_requests_queue_until_first_two_settle_under_same_one_dollar_cap(self):
        async def run(client, backend, ledger):
            body = {"messages": [{"role": "user", "content": "grade fixture"}]}
            jobs = [
                asyncio.create_task(client.post("/runs/test/judge/v1/chat/completions", json=body)) for _ in range(3)
            ]
            await asyncio.wait_for(backend.started.wait(), 2)
            await asyncio.sleep(0.02)
            self.assertEqual(len(backend.calls), 2)
            self.assertEqual(len(json.loads(ledger.read_text())["pending"]), 2)
            backend.release.set()
            replies = await asyncio.wait_for(asyncio.gather(*jobs), 2)
            self.assertEqual([reply.status_code for reply in replies], [200, 200, 200])
            self.assertEqual(backend.peak, 2)
            state = json.loads(ledger.read_text())
            self.assertEqual(state["budget_usd"], 1)
            self.assertEqual(state["requests_settled"], 3)
            self.assertEqual(state["pending"], {})
            self.assertAlmostEqual(state["cost_upper_bound_usd"], 0.0003024 * 3)

        self.exercise(run)

    def test_cancelled_paid_request_settles_conservatively_and_releases_slot(self):
        async def run(client, backend, ledger):
            body = {"messages": []}
            job = asyncio.create_task(client.post("/runs/test/judge/v1/chat/completions", json=body))
            for _ in range(100):
                if backend.calls:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(len(backend.calls), 1)
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
            state = json.loads(ledger.read_text())
            self.assertEqual(state["pending"], {})
            self.assertEqual(state["requests_settled"], 1)
            self.assertEqual(state["missing_usage_requests"], 1)
            self.assertGreater(state["cost_upper_bound_usd"], 0.33)
            backend.release.set()
            response = await client.post("/runs/test/judge/v1/chat/completions", json=body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(ledger.read_text())["pending"], {})

        self.exercise(run)


if __name__ == "__main__":
    unittest.main()
