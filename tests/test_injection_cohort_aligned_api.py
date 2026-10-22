import asyncio
import copy
import json
from types import SimpleNamespace

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
