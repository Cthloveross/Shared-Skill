import asyncio
import copy
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from scripts.injection import e2e_30
from scripts.injection.cohort_aligned_api import BoundAgentRoutes
import scripts.injection.cohort_aligned_api as bridge


async def invoke(app, path, body):
    events = iter([{"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}])
    sent = []

    async def receive():
        return next(events, {"type": "http.disconnect"})

    async def send(event):
        sent.append(event)

    await app({"type": "http", "method": "POST", "path": path, "raw_path": path.encode(),
               "query_string": b"", "headers": [(b"content-type", b"application/json"),
               (b"x-session-id", b"spoofed")]}, receive, send)
    return sent


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    e2e_30.e2e.load_components()
    from scripts.wildclawbench import alignment_catalog
    bank = tmp_path / "bank/demo"
    bank.mkdir(parents=True)
    (bank / "SKILL.md").write_text("---\nname: demo\ndescription: A valid shared skill.\n---\n# Demo\n")
    settings = {"runtime_dir": str(tmp_path / "runtime"), "protocol_hash": "protocol",
                "alignment": {"version": "native-night-atomic-v1"}}
    binding = alignment_catalog.prepare_binding(settings, "run1", "exec1", bank.parent)
    seen = []

    async def native(scope, receive, send):
        seen.append({"path": scope["path"], "headers": dict(scope["headers"]),
                     "body": json.loads((await receive())["body"])})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"native"})

    service = tmp_path / "day-03.01"
    service.mkdir()
    return BoundAgentRoutes(native, settings, binding, "http://gateway", service), seen, bank


def test_registered_agent_route_keeps_original_body_and_forces_own_session(proxy):
    app, seen, _ = proxy
    body = {"messages": [{"role": "user", "content": "task"}], "tools": [{"type": "function"}]}
    original = copy.deepcopy(body)
    sent = asyncio.run(invoke(app, app.bound + "/chat/completions", body))
    assert sent[0]["status"] == 200 and len(seen) == 1
    assert seen[0]["path"] == "/v1/chat/completions"
    assert seen[0]["headers"][b"x-session-id"] == b"day-03.01-chat"
    assert seen[0]["headers"][b"x-turn-type"] == b"main"
    assert seen[0]["body"] == original
    row = json.loads((app.service / "routing.jsonl").read_text())
    assert row["catalog_sha256"] == app.binding["catalog_sha256"]
    assert row["destination"] == "sharedskill_agent"


def test_changed_bank_or_cross_execution_never_enters_native_agent(proxy):
    app, seen, bank = proxy
    wrong = asyncio.run(invoke(app, "/runs/run1/executions/other/agent/v1/chat/completions", {}))
    assert wrong[0]["status"] == 409 and not seen
    (bank / "SKILL.md").write_text("changed after binding")
    changed = asyncio.run(invoke(app, app.bound + "/chat/completions", {}))
    assert changed[0]["status"] == 409 and not seen


def test_tool_and_judge_requests_bypass_native_session_and_catalog(proxy, monkeypatch):
    app, seen, _ = proxy
    calls = []

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, url, headers, content):
            calls.append((url, json.loads(content)))
            return SimpleNamespace(status_code=200, headers={}, content=b"judge")

    monkeypatch.setattr(bridge.httpx, "AsyncClient", Client)
    body = {"model": "judge", "messages": [{"role": "user", "content": "grade"}]}
    sent = asyncio.run(invoke(app, app.unbound + "/chat/completions", body))
    assert sent[-1]["body"] == b"judge" and not seen
    assert calls == [("http://gateway" + app.unbound + "/chat/completions", body)]


def test_local_judge_route_records_policy_and_rejects_another_run(proxy, monkeypatch):
    app, seen, _ = proxy
    settings = {**app.settings, "model_id": "local-model", "local_judge_enable_thinking": False,
                "local_judge_max_tokens": 2048, "gateway_request_timeout_seconds": 7}
    app = BoundAgentRoutes(app.app, settings, app.binding, app.gateway, app.service)
    calls = []

    class Client:
        def __init__(self, **kwargs): assert kwargs['timeout'] == 7
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, url, headers, content):
            calls.append((url, json.loads(content)))
            return SimpleNamespace(status_code=200, headers={}, content=b'judge')

    monkeypatch.setattr(bridge.httpx, 'AsyncClient', Client)
    body = {'messages': [{'role': 'user', 'content': 'grade'}]}
    result = asyncio.run(invoke(app, app.local_judge + '/chat/completions', body))
    assert result[-1]['body'] == b'judge' and not seen
    assert calls == [(app.gateway + app.local_judge + '/chat/completions', body)]
    row = json.loads((app.service / 'routing.jsonl').read_text())
    assert row['purpose'] == 'local_judge' and row['session_id'] is None
    assert row['scoring_policy'] == {'model_id': 'local-model', 'protocol_hash': 'protocol',
                                    'enable_thinking': False, 'max_tokens': 2048}
    wrong = asyncio.run(invoke(app, '/runs/other/local-judge/v1/chat/completions', body))
    assert wrong[0]['status'] == 409 and len(calls) == 1


def test_local_judge_unconfigured_does_not_bypass_bound_route(proxy):
    app, seen, _ = proxy
    result = asyncio.run(invoke(app, app.local_judge + '/chat/completions', {}))
    assert result[0]['status'] == 409 and not seen


@pytest.fixture
def real_recording_api(tmp_path, monkeypatch):
    bank = tmp_path / "bank/demo"
    bank.mkdir(parents=True)
    (bank / "SKILL.md").write_text("---\nname: demo\ndescription: Test skill\n---\n# Demo\n")
    settings = {"model_id": "Qwen3.5-35B-A3B", "context_length": 8192,
        "alignment": {"version": "native-night-atomic-v1", "catalog_max_chars": 4321},
        "runtime_dir": str(tmp_path / "runtime"), "protocol_hash": "test-protocol",
        "local_api_key": "local-e2e-only"}
    # Explicit runtime settings must work even when the source config no longer exists.
    monkeypatch.setattr(bridge.e2e, "CONFIG_FILE", tmp_path / "does-not-exist.json")
    return bank.parent, settings


def test_build_real_sharedskill_config_uses_runtime_model_context_without_api_port(tmp_path, real_recording_api):
    from sharedskill.config import SharedSkillConfig
    from sharedskill.api_server import SharedSkillAPIServer
    bank, settings = real_recording_api
    service = tmp_path / "service"
    bridge.e2e.write_json(service / "gateway.json", {"base_url": "http://127.0.0.1:9/v1", "root": "http://127.0.0.1:9"})
    api = bridge.e2e.build_api(bank, service / "objects", service / "records", service / "requests", runtime_settings=settings)
    assert isinstance(api, SharedSkillAPIServer) and isinstance(api.config, SharedSkillConfig)
    assert api.config.model_name == api.config.llm_model_id == api.config.served_model_name == settings["model_id"]
    assert api.config.max_context_tokens == 8192 and api.config.max_skills_prompt_chars == 4321
    assert api.config.proxy_port == 0


def test_real_aligned_api_allocates_ephemeral_port_and_serves_current_model(tmp_path, real_recording_api):
    bank, settings = real_recording_api
    task = {"task_id": "task", "user_id": "user-01", "source_path": str(tmp_path / "task.md")}
    settings = {**settings, "user_id": task["user_id"], "task": {"source_path": task["source_path"]}}
    api, port, service = bridge.start_aligned_api(bank, tmp_path, "day-test", "http://127.0.0.1:9",
                                                 settings, task, tmp_path / "output")
    try:
        assert 0 < port <= 65535 and api.config.proxy_port == port
        response = httpx.get(f"http://127.0.0.1:{port}{api.app.bound}/models",
                             headers={"Authorization": "Bearer local-e2e-only"}, timeout=2, trust_env=False)
        assert response.status_code == 200
        assert response.json()["data"][0]["id"] == "Qwen3.5-35B-A3B"
        assert api.config.max_context_tokens == 8192
        assert json.loads((service / "service.json").read_text())["port"] == port
    finally:
        bridge.e2e.stop_api(api)


def test_real_plain_api_reads_frozen_runtime_and_avoids_busy_preferred_port(tmp_path, real_recording_api):
    bank, settings = real_recording_api
    with socket.socket() as occupied:
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        settings = {**settings, "api_port": occupied.getsockname()[1]}
        bridge.e2e.write_json(tmp_path / "config.runtime.json", settings)
        api, port, _ = bridge.e2e.start_api(bank, tmp_path, "plain-test", "http://127.0.0.1:9")
        try:
            assert port > 0 and port != settings["api_port"]
            response = httpx.get(f"http://127.0.0.1:{port}/v1/models",
                                 headers={"Authorization": "Bearer local-e2e-only"}, timeout=2, trust_env=False)
            assert response.status_code == 200
            assert response.json()["data"][0]["id"] == settings["model_id"]
        finally:
            bridge.e2e.stop_api(api)
