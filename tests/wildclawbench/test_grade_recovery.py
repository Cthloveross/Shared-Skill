"""Independent grading recovery: immutable execution, policy-specific replay, no models."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import types
import urllib.request

import httpx
import pytest
import requests

from scripts.wildclawbench import alignment_catalog, alignment_snapshot, grade_recovery, grading_cache
from scripts.wildclawbench import task_runtime as runtime
from scripts.wildclawbench.protocol import canonical_hash, write_json


def response(content='{"score": 1}', finish="stop"):
    return json.dumps({"choices": [{"finish_reason": finish, "message": {"content": content}}]}).encode()


def source_fixture(root, *, completed=False, successful=False):
    output = root / "source"
    (output / "workspace").mkdir(parents=True)
    (output / "workspace" / "answer.txt").write_text("sealed Agent answer")
    bank = root / "bank"
    (bank / "audit").mkdir(parents=True)
    (bank / "audit" / "SKILL.md").write_text("---\nname: audit\ndescription: audit test\n---\nCheck work.\n")
    config = {"model_id": "test-qwen", "model_revision": "fixed", "max_tokens": 4096,
              "enable_thinking": True, "context_length": 32000, "seed": 42,
              "protocol_hash": "frozen-protocol", "alignment": {"version": "test"},
              "runtime_dir": str(root / "runtime"), "gateway_log_dir": str(root / "gateway"),
              "phase": "day", "round": 1, "user_id": "user1", "grading_timeout_seconds": 300,
              "case_timeout_seconds": 300, "local_api_base_url": "http://old-gateway/v1"}
    binding = alignment_catalog.prepare_binding(config, "old-execution", "execution-one", bank)
    write_json(output / "runtime-config.json", config)
    write_json(output / "execution-binding.json", binding)
    write_json(output / "agent-quiescence.json", {"quiescent": True, "surviving_writers": []})
    (output / "chat.jsonl").write_text(json.dumps({"message": {"role": "assistant", "content": "finished"}}) + "\n")
    checks = "def grade(**kwargs): return {'overall_score': .75}"
    (output / "automated-checks.py").write_text(checks)
    (output / "execution-prompt.txt").write_text("original complete task prompt")
    write_json(output / "task-variant.json", {
        "modified_prompt_sha256": hashlib.sha256((output / "execution-prompt.txt").read_bytes()).hexdigest(),
        "modified_checks_sha256": hashlib.sha256(checks.encode()).hexdigest()})
    write_json(output / "mock-replay.json", {"http://localhost:9110/audit": {"status": 200, "body": "[]"}})
    identity = alignment_snapshot.execution_identity("03_fixture", "old-execution", binding, config)
    agent = alignment_snapshot.freeze_agent_output(output, identity)
    recovery = alignment_snapshot.seal_grading_recovery(output, identity, "", [])
    write_json(output / "execution.json", {"status": "agent_completed", "task_id": "03_fixture",
        "output_snapshot_sha256": agent["manifest_sha256"], "grading_recovery_sha256": recovery["manifest_sha256"]})
    write_json(output / "runtime-result.json", {"task_id": "03_fixture", "run_id": "old-execution",
        "status": "completed" if completed else "pending_score", "score": .375 if completed else None})
    if successful:
        url = "http://old-gateway/runs/old-execution/v1/chat/completions"
        raw = json.dumps({"model": "test-qwen", "messages": [{"role": "user", "content": "score"}],
                          "response_format": {"type": "json_object"}}).encode()
        event = {"kind": "model_api", "status": 200, "error": None, "url": url,
                 "fingerprint": hashlib.sha256(url.encode() + raw).hexdigest()}
        (output / "judge-api.jsonl").write_text(json.dumps(event) + "\n")
        evidence = Path(config["gateway_log_dir"]) / binding["run_id"] / "request"
        write_json(evidence / "record.json", {"status": "completed", "http_status": 200,
            "protocol_hash": config["protocol_hash"], "actual_model": config["model_id"]})
        (evidence / "incoming.json").write_bytes(raw)
        (evidence / "response.bin").write_bytes(response())
    return output


def overrides(**extra):
    return {"grading_recovery_max_requests": 8, **extra}


def all_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_deterministic_result_reuses_without_agent_or_grader_and_is_idempotent(tmp_path, monkeypatch):
    source = source_fixture(tmp_path, completed=True)
    original = all_bytes(source)
    attempt = tmp_path / "attempt"
    receipt = grade_recovery.prepare_recovery(source, attempt, overrides(
        local_judge_enable_thinking=False, local_judge_max_tokens=4096))
    assert receipt["deterministic_reuse"] and receipt["judge_policy_changed"] and not receipt["full_rejudge"]
    monkeypatch.setattr(runtime, "execute_task", lambda *a: pytest.fail("No new Agent or grader allowed"))
    assert grade_recovery.run_recovery(attempt)["score"] == .375
    assert grade_recovery.run_recovery(attempt)["score"] == .375
    final = grade_recovery.verify_recovery(attempt)
    assert final["source_run_id"] == "old-execution"
    assert final["scoring_run_id"] != final["source_run_id"]
    assert final["new_agent_executions"] == 0
    assert all_bytes(source) == original


def test_same_policy_replays_success_but_changed_policy_requires_full_rejudge(tmp_path):
    source = source_fixture(tmp_path, successful=True)
    same = grade_recovery.prepare_recovery(source, tmp_path / "same", overrides())
    assert same["cached_successful_requests"] == 1 and not same["full_rejudge"]
    changed = grade_recovery.prepare_recovery(source, tmp_path / "changed", overrides(
        local_judge_enable_thinking=False, local_judge_max_tokens=4096))
    assert changed["cached_successful_requests"] == 0 and changed["full_rejudge"]
    cache = json.loads((tmp_path / "same" / "model-replay.json").read_text())
    request = (tmp_path / "gateway" / "old-execution" / "request" / "incoming.json").read_bytes()
    assert grading_cache.request_key("http://new-gateway/runs/new/local-judge/v1/chat/completions",
                                    request, same["judge_policy_sha256"]) in cache["entries"]


@pytest.mark.parametrize("name", ["workspace/answer.txt", "chat.jsonl", "model-replay.json", "runtime-config.json"])
def test_attempt_tampering_fails_closed(tmp_path, name):
    source = source_fixture(tmp_path)
    attempt = tmp_path / "attempt"
    grade_recovery.prepare_recovery(source, attempt, overrides())
    (attempt / name).write_text("changed")
    with pytest.raises((ValueError, alignment_snapshot.SnapshotError)):
        grade_recovery.verify_recovery(attempt, require_result=False)


def test_source_tampering_and_agent_config_overrides_are_rejected(tmp_path):
    source = source_fixture(tmp_path)
    attempt = tmp_path / "attempt"
    with pytest.raises(ValueError, match="only explicit judge"):
        grade_recovery.prepare_recovery(source, attempt, overrides(required_shared_skill="audit"))
    grade_recovery.prepare_recovery(source, attempt, overrides())
    (source / "runtime-result.json").write_text("{}")
    with pytest.raises(ValueError, match="Original"):
        grade_recovery.verify_recovery(attempt, require_result=False)


def test_started_attempt_cannot_silently_receive_a_new_allowance(tmp_path, monkeypatch):
    source = source_fixture(tmp_path)
    attempt = tmp_path / "attempt"
    grade_recovery.prepare_recovery(source, attempt, overrides())
    def fail(*args):
        raise RuntimeError("interrupted")
    monkeypatch.setattr(runtime, "execute_task", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        grade_recovery.run_recovery(attempt)
    with pytest.raises(ValueError, match="already started"):
        grade_recovery.run_recovery(attempt)


def test_real_runtime_rescore_uses_original_binding_and_never_executes_agent(tmp_path, monkeypatch):
    source = source_fixture(tmp_path)
    attempt = tmp_path / "attempt"
    grade_recovery.prepare_recovery(source, attempt, overrides(
        local_judge_enable_thinking=False, local_judge_max_tokens=4096))
    work = tmp_path / "task-input"
    (work / "exec").mkdir(parents=True)
    spec = {"task_id": "03_fixture", "prompt": "task", "timeout_seconds": 300,
            "workspace_path": str(work), "automated_checks": "unused", "warmup": ""}
    calls = []
    def docker(args, *a, **kw):
        calls.append(args)
        assert not ("openclaw" in args and "agent" in args), "Agent was rerun"
        return subprocess.CompletedProcess(args, 0, "", "")
    grading = types.ModuleType("src.utils.grading")
    grading.extract_usage_from_jsonl = lambda path: {}
    monkeypatch.setitem(sys.modules, "src.utils.grading", grading)
    monkeypatch.setattr(runtime.environment, "paths", lambda c: {"upstream": tmp_path, "assets": tmp_path / "assets"})
    monkeypatch.setattr(runtime.environment, "container_api_url", lambda *a, **kw: "http://new/v1")
    monkeypatch.setattr(runtime.environment, "openclaw_config", lambda *a, **kw: {})
    monkeypatch.setattr(runtime.environment, "apply_python_bootstrap", lambda *a, **kw: None)
    monkeypatch.setattr(runtime.environment, "apply_local_tool_adapters", lambda *a, **kw: None)
    monkeypatch.setattr(runtime, "task_spec", lambda *a: spec)
    monkeypatch.setattr(runtime, "_docker", docker)
    monkeypatch.setattr(runtime, "_copy_in", lambda *a, **kw: None)
    monkeypatch.setattr(runtime, "_grade", lambda *a, **kw: ({"overall_score": .75}, []))
    original = all_bytes(source)
    monkeypatch.setattr(runtime, "execute_task", lambda task, bank, out, conf:
                        runtime._execute_child(task, bank, out, conf, True))
    result = grade_recovery.run_recovery(attempt)
    assert result["status"] == "completed", result.get("error")
    assert result["score"] == .75 and result["rescore"]
    assert grade_recovery.verify_recovery(attempt)["source_run_id"] == "old-execution"
    assert all_bytes(source) == original
    assert any(command[:2] == ["rm", "--force"] for command in calls)


@pytest.fixture
def installed_cache(tmp_path, monkeypatch):
    # install() intentionally wraps the real libraries as in the private grader;
    # register restoration first so these wrappers never leak into other tests.
    for owner, name in ((httpx.Client, "send"), (requests.Session, "send"), (urllib.request, "urlopen")):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    def install(entries=None, limit=2, policy="same"):
        cache, captures = tmp_path / "cache.json", tmp_path / "captures.json"
        write_json(cache, {"judge_policy_sha256": policy, "max_new_requests": limit, "entries": entries or {}})
        events = []
        grading_cache.install({"_is_local_model_api": lambda url: "/v1/" in url,
                               "_audit": lambda *a, **kw: events.append(kw)}, str(cache), str(captures))
        return captures, events
    return install


def test_exact_success_cache_skips_backend_missing_request_calls_once(installed_cache):
    old = b'{"model":"q","messages":[{"role":"user","content":"old"}]}'
    new = b'{"model":"q","messages":[{"role":"user","content":"new"}]}'
    url = "http://fake/v1/chat/completions"
    key = grading_cache.request_key(url, old, "same")
    captures, audit = installed_cache({key: {"status": 200, "body": response().decode()}})
    requests_sent = []
    def backend(request):
        requests_sent.append(request.content)
        return httpx.Response(200, content=response())
    with httpx.Client(transport=httpx.MockTransport(backend)) as client:
        assert client.post(url, content=old).status_code == 200
        assert client.post(url, content=new).status_code == 200
        assert client.post(url, content=new).status_code == 200
    assert requests_sent == [new]
    assert len(audit) == 2 and all(event["cached_response"] for event in audit)
    assert json.loads(captures.read_text())["requests"] == 1


def test_failed_requests_keep_allowance_and_are_not_cached(installed_cache):
    captures, audit = installed_cache(limit=2)
    sent = []
    def backend(request):
        sent.append(1)
        return httpx.Response(200, content=response("", "length"))
    with httpx.Client(transport=httpx.MockTransport(backend)) as client:
        for _ in range(2):
            client.post("http://fake/v1/chat/completions", json={"model": "q"})
        with pytest.raises(RuntimeError, match="budget exhausted"):
            client.post("http://fake/v1/chat/completions", json={"model": "q"})
    assert len(sent) == 2
    assert json.loads(captures.read_text())["requests"] == 2
    assert audit[-1]["error"]


def test_transport_error_keeps_reservation(installed_cache):
    captures, _ = installed_cache(limit=1)
    def fail(request):
        raise httpx.ConnectError("offline", request=request)
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(httpx.ConnectError):
            client.post("http://fake/v1/chat/completions", json={})
        with pytest.raises(RuntimeError, match="budget exhausted"):
            client.post("http://fake/v1/chat/completions", json={})
    assert json.loads(captures.read_text())["requests"] == 1


def test_invalid_json_and_truncated_responses_do_not_certify_success():
    request = b'{"response_format":{"type":"json_object"}}'
    assert grading_cache.successful_response(200, response(), request)
    assert not grading_cache.successful_response(200, response("not JSON"), request)
    assert not grading_cache.successful_response(200, response("{}", "length"), request)
    assert not grading_cache.successful_response(200, response(""), request)
    assert not grading_cache.successful_response(503, response(), request)
    assert grading_cache.request_key("http://a/v1/chat/completions", request, "A") != \
        grading_cache.request_key("http://a/v1/chat/completions", request, "B")


@pytest.mark.parametrize("library", ["requests", "urllib"])
def test_successful_replay_supports_other_official_grader_clients(installed_cache, library):
    url, body = "http://fake/v1/chat/completions", b'{"model":"q"}'
    key = grading_cache.request_key(url, body, "same")
    installed_cache({key: {"status": 200, "body": response().decode()}})
    if library == "requests":
        assert requests.post(url, data=body).status_code == 200
    else:
        with urllib.request.urlopen(urllib.request.Request(url, data=body)) as result:
            assert result.status == 200
