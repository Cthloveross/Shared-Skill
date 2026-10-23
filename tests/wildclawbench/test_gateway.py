"""Routing tests prove model aliases and image/audio requests remain local."""

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import gateway
import httpx
import pytest
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


def test_text_only_interfaces_probe_only_main_and_reject_media(tmp_path):
    config = {**load_config(), 'runtime_dir': str(tmp_path / 'runtime'), 'required_interfaces': ['text', 'tools'],
              'gateway_log_dir': str(tmp_path / 'outputs')}
    gateway.create_app(config)
    assert (tmp_path / 'outputs/router-config.json').is_file()
    assert not (tmp_path / 'runtime/api-evidence').exists()
    app = gateway.create_app(config)
    seen = []
    async def get(self, url, **kwargs):
        seen.append(url)
        return httpx.Response(200, json={'data': [{'id': config['model_id']}]})
    with TestClient(app) as client, patch.object(httpx.AsyncClient, 'get', get):
        health = client.get('/healthz').json()
        assert health['ready'] and set(health['roles']) == {'main'}
        assert len(seen) == 1 and '18181' in seen[0]
        response = client.post('/v1/images/generations', json={'prompt': 'unused'})
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'interface_not_enabled'


def test_gateway_health_binding_is_checked_before_agent_start(tmp_path):
    from scripts.wildclawbench import run
    config = {**load_config(), 'runtime_dir': str(tmp_path), 'required_interfaces': ['text', 'tools'],
              'protocol_hash': 'frozen-media', 'alignment': {'version': 'native-night-atomic-v1'}}
    app = gateway.create_app(config)
    async def get(self, url, **kwargs):
        return httpx.Response(200, json={'data': [{'id': config['model_id']}]})
    with TestClient(app) as client, patch.object(httpx.AsyncClient, 'get', get):
        health = client.get('/healthz')
        assert health.json()['protocol_hash'] == config['protocol_hash']
        with patch.object(run.httpx, 'get', return_value=health):
            run.check_services(config)
            with pytest.raises(RuntimeError, match='Gateway protocol differs'):
                run.check_services({**config, 'protocol_hash': 'different-run'})
        missing = httpx.Response(200, json={'ready': True}, request=httpx.Request('GET', 'http://local/healthz'))
        with patch.object(run.httpx, 'get', return_value=missing):
            with pytest.raises(RuntimeError, match='Gateway protocol differs'):
                run.check_services(config)
            run.check_services({**config, 'protocol_hash': None})


@pytest.mark.parametrize('image_count', [0, 10])
def test_interface_check_uses_auto_tools_and_roundtrip_without_media(tmp_path, image_count):
    from scripts.wildclawbench import interface_checks
    config = {**load_config(), 'runtime_dir': str(tmp_path), 'protocol_hash': 'probe',
              'required_interfaces': ['text', 'tools'], 'interface_request_timeout_seconds': 4}
    if image_count:
        config.update(required_interfaces=['text', 'tools', 'vision'], interface_vision_image_count=image_count)
    requests = []
    def post(self, url, **kwargs):
        body = kwargs['json']; requests.append(body)
        assert body['chat_template_kwargs']['enable_thinking'] is False
        if len(requests) == 1:
            message = {'content': 'LOCAL_READY'}
        elif len(requests) == 2:
            assert body['tool_choice'] == 'auto'
            message = {'content': None, 'tool_calls': [{'id': 'tool-1', 'type': 'function',
                       'function': {'name': 'local_probe', 'arguments': '{"text":"LOCAL_READY"}'}}]}
        elif len(requests) == 3:
            assert body['messages'][-1]['tool_call_id'] == 'tool-1'
            message = {'content': 'TOOL_ROUNDTRIP_OK'}
        else:
            content = body['messages'][0]['content']
            assert len([part for part in content if part['type'] == 'image_url']) == image_count
            message = {'content': 'red'}
        return httpx.Response(200, json={'choices': [{'message': message}]}, request=httpx.Request('POST', url))
    with patch.object(httpx.Client, 'post', post):
        result = interface_checks.verify_interfaces(config, tmp_path / 'interfaces')
    assert result['passed'] and len(requests) == (4 if image_count else 3)
    assert set(result['checks']) == {'text', 'tools', 'tools-roundtrip'} | ({'vision'} if image_count else set())


def test_local_budget_blocks_before_upstream_and_survives_router_restart(tmp_path):
    config = {**load_config(), 'runtime_dir': str(tmp_path),
              'required_interfaces': ['text', 'tools'],
              'local_request_budget': {'path': str(tmp_path / 'budget.json'), 'max_requests': 1,
                                       'max_tokens': 131072}}
    LocalBackend.calls = []
    async def counted(*args, **kwargs):
        return {'remaining_tokens': 1}
    for expected in (200, 429):
        with TestClient(gateway.create_app(config)) as client, patch.object(gateway.httpx, 'AsyncClient', LocalBackend), \
             patch.object(gateway, 'main_context_budget', side_effect=counted):
            result = client.post('/runs/budget-check/v1/chat/completions', json={'messages': []})
            assert result.status_code == expected
    assert len(LocalBackend.calls) == 1
    assert json.loads((tmp_path / 'budget.json').read_text())['requests'] == 1


@contextmanager
def _tcp_servers():
    """Run isolated real HTTP servers and always join their threads."""
    import socket
    import threading
    import time
    import uvicorn
    servers, threads, sockets = [], [], []
    def start(app):
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen(128)
        sockets.append(sock)
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off', ws='none'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        servers.append(server); threads.append(thread); thread.start()
        limit = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < limit:
            time.sleep(.01)
        assert server.started
        return 'http://127.0.0.1:' + str(sock.getsockname()[1])
    try:
        yield start
    finally:
        for server in servers:
            server.should_exit = True
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        for sock in sockets:
            sock.close()


def test_real_http_gateway_budget_and_tool_contract(tmp_path):
    """Real TCP proxy and tokenizer; no model inference or shared services."""
    from fastapi import FastAPI

    upstream = FastAPI()
    inferred, tokenized = [], []
    @upstream.post('/tokenize')
    async def tokenize(body: dict):
        tokenized.append(body)
        count = 200000 if body.get('messages', [{}])[0].get('content') == 'oversized' else 20
        return {'count': count, 'max_model_len': 131072}
    @upstream.post('/v1/chat/completions')
    async def chat(body: dict):
        inferred.append(body)
        return {'choices': [{'message': {'content': None, 'tool_calls': [
            {'id': 'call-live', 'type': 'function', 'function': {'name': 'echo', 'arguments': '{"value":"ready"}'}}
        ]}}]}

    with _tcp_servers() as start:
        backend = start(upstream)
        cfg = {**load_config(), 'runtime_dir': str(tmp_path), 'required_interfaces': ['text', 'tools'],
               'model_base_url': backend + '/v1', 'agent_tokenizer_url': backend + '/tokenize',
               'local_request_budget': {'path': str(tmp_path / 'budget.json'), 'max_requests': 1, 'max_tokens': 131072}}
        endpoint = start(gateway.create_app(cfg))
        body = {'messages': [{'role': 'user', 'content': 'Use echo.'}], 'tool_choice': 'auto',
                'tools': [{'type': 'function', 'function': {'name': 'echo', 'parameters': {'type': 'object'}}}]}
        with httpx.Client(base_url=endpoint, timeout=5, trust_env=False) as client:
            for key in ('n', 'best_of'):
                assert client.post('/v1/chat/completions', json={**body, key: 2}).status_code == 400
            assert client.post('/v1/completions', json={'prompt': 'unsupported'}).status_code == 400
            assert not tokenized and not inferred
            large = client.post('/v1/chat/completions', json={**body, 'messages': [{'role': 'user', 'content': 'oversized'}]})
            assert large.status_code == 400 and not inferred
            assert not (tmp_path / 'budget.json').exists()
            first = client.post('/v1/chat/completions', json=body)
            assert first.status_code == 200 and first.json()['choices'][0]['message']['tool_calls'][0]['id'] == 'call-live'
            assert len(inferred) == 1 and len(tokenized) == 2
            assert inferred[0]['tool_choice'] == 'auto' and inferred[0]['tools'] == body['tools']
            blocked = client.post('/v1/chat/completions', json=body)
            assert blocked.status_code == 429 and blocked.json()['error']['code'] == 'local_budget_exhausted'
            assert client.post('/v1/images/generations', json={'prompt': 'disabled'}).status_code == 503
            assert client.post('/v1/audio/speech', json={'input': 'disabled'}).status_code == 503
            assert len(inferred) == 1
        assert json.loads((tmp_path / 'budget.json').read_text())['requests'] == 1


def test_real_http_local_judge_policy_is_separate_frozen_and_budgeted(tmp_path):
    from fastapi import FastAPI
    from scripts.injection.cohort_aligned_api import BoundAgentRoutes

    upstream = FastAPI()
    requests, tokenized = [], []
    @upstream.post('/tokenize')
    async def tokenize(body: dict):
        tokenized.append(body)
        return {'count': 20, 'max_model_len': 131072}
    @upstream.post('/v1/chat/completions')
    async def chat(body: dict):
        requests.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"score": 1}'}}]}

    with _tcp_servers() as start:
        backend = start(upstream)
        config = {**load_config(), 'runtime_dir': str(tmp_path), 'required_interfaces': ['text', 'tools'],
                  'protocol_hash': 'test-judge-protocol',
                  'model_base_url': backend + '/v1', 'agent_tokenizer_url': backend + '/tokenize',
                  'enable_thinking': True, 'max_tokens': 4096, 'interface_max_tokens': 128,
                  'local_judge_enable_thinking': False, 'local_judge_max_tokens': 2048,
                  'local_request_budget': {'path': str(tmp_path / 'budget.json'),
                                           'max_requests': 3, 'max_tokens': 3 * 131072}}
        endpoint = start(gateway.create_app(config))
        service = tmp_path / 'cohort-service'
        service.mkdir()
        native_calls = []
        async def native(scope, receive, send):
            native_calls.append(scope)
            raise AssertionError('Grader requests must not enter the native Agent recorder')
        bridge = BoundAgentRoutes(native, config, {'run_id': 'interface-checks', 'execution_id': 'exec1',
                                  'bank_sha256': 'bank', 'catalog_sha256': 'catalog', 'binding_sha256': 'binding'},
                                  endpoint, service)
        cohort_endpoint = start(bridge)
        body = {'model': 'remote-alias', 'messages': [{'role': 'user', 'content': 'score'}],
                'max_tokens': 999999, 'max_completion_tokens': 1,
                'chat_template_kwargs': {'enable_thinking': True}, 'chat_template': 'override',
                'thinking': True, 'reasoning': {'effort': 'high'}, 'enable_thinking': True,
                'response_format': {'type': 'json_object'}}
        with httpx.Client(base_url=endpoint, timeout=5, trust_env=False) as client:
            # Even the interface-checks run ID cannot replace the real grading policy.
            url = '/runs/interface-checks/local-judge/v1/chat/completions'
            assert client.post(cohort_endpoint + url, json=body).status_code == 200
            assert client.post(cohort_endpoint + '/runs/other/local-judge/v1/chat/completions', json=body).status_code == 409
            judge = requests[-1]
            assert judge['max_tokens'] == 2048 and judge['chat_template_kwargs']['enable_thinking'] is False
            assert judge['response_format'] == body['response_format']
            assert not {'max_completion_tokens', 'chat_template', 'thinking', 'reasoning', 'enable_thinking'} & judge.keys()
            assert tokenized[-1]['chat_template_kwargs']['enable_thinking'] is False
            assert client.post('/runs/day/v1/chat/completions', json={**body, 'local_judge': True}).status_code == 200
            assert requests[-1]['max_tokens'] == 4096 and requests[-1]['chat_template_kwargs']['enable_thinking'] is True
            assert client.post('/runs/interface-checks/v1/chat/completions', json=body).status_code == 200
            assert requests[-1]['max_tokens'] == 128 and requests[-1]['chat_template_kwargs']['enable_thinking'] is False
            assert client.post(cohort_endpoint + url, json=body).status_code == 429
            assert len(requests) == 3
        assert not native_calls
        routes = [json.loads(line) for line in (service / 'routing.jsonl').read_text().splitlines()]
        assert len(routes) == 2 and all(row['purpose'] == 'local_judge' and row['session_id'] is None for row in routes)
        assert routes[0]['scoring_policy']['max_tokens'] == 2048
        records = [json.loads(file.read_text()) for file in (tmp_path / 'api-evidence').glob('*/*/record.json')]
        judge_record = next(record for record in records if record['purpose'] == 'local_judge' and record['status'] == 'completed')
        assert judge_record['role'] == 'main' and judge_record['local_judge_settings'] == {'enable_thinking': False, 'max_tokens': 2048}


def test_local_judge_route_is_opt_in_and_does_not_change_legacy_policy(tmp_path):
    config = {**load_config(), 'runtime_dir': str(tmp_path)}
    with TestClient(gateway.create_app(config)) as client:
        result = client.post('/local-judge/v1/chat/completions', json={'messages': []})
    assert result.status_code == 404


def test_real_media_budget_byte_limit_and_deadline_forwarding(tmp_path):
    from fastapi import FastAPI, Request
    upstream = FastAPI()
    calls = []
    @upstream.post('/v1/images/generations')
    async def generate(request: Request):
        calls.append(dict(request.headers))
        return {'data': []}
    with _tcp_servers() as start:
        backend = start(upstream)
        cfg = {**load_config(), 'runtime_dir': str(tmp_path),
               'image_base_url': backend + '/v1', 'gateway_max_request_bytes': 200,
               'media_request_timeout_seconds': 2,
               'media_request_budget': {'path': str(tmp_path / 'media.json'), 'max_requests': 1}}
        endpoint = start(gateway.create_app(cfg))
        with httpx.Client(base_url=endpoint, timeout=5, trust_env=False) as client:
            assert client.post('/v1/images/generations', json={'prompt': 'x' * 300}).status_code == 413
            assert not calls and not (tmp_path / 'media.json').exists()
            assert client.post('/runs/test-media/v1/images/generations', json={'prompt': 'ok'}).status_code == 200
            assert calls[0]['x-run-id'] == 'test-media'
            assert float(calls[0]['x-request-deadline-unix']) > 0
            assert client.post('/v1/images/generations', json={'prompt': 'blocked'}).status_code == 429
            assert len(calls) == 1


def test_real_media_client_disconnect_reaches_backend(tmp_path):
    import asyncio
    import threading
    import time
    from fastapi import FastAPI, Request
    upstream = FastAPI()
    disconnected = threading.Event()
    @upstream.post('/v1/images/generations')
    async def generate(request: Request):
        await request.body()
        for _ in range(100):
            if await request.is_disconnected():
                disconnected.set()
                break
            await asyncio.sleep(.02)
        return {'data': []}
    with _tcp_servers() as start:
        backend = start(upstream)
        cfg = {**load_config(), 'runtime_dir': str(tmp_path), 'image_base_url': backend + '/v1',
               'media_request_timeout_seconds': 5}
        endpoint = start(gateway.create_app(cfg))
        with httpx.Client(base_url=endpoint, timeout=.3, trust_env=False) as client:
            try:
                client.post('/v1/images/generations', json={'prompt': 'cancel me'})
            except httpx.ReadTimeout:
                pass
            else:
                raise AssertionError('Expected downstream timeout')
        assert disconnected.wait(1), 'backend kept working after downstream disconnected'
