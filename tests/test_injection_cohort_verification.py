import json
from pathlib import Path

import pytest

from scripts.injection import cohort_verification as verifier


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def proof(tmp_path, monkeypatch):
    run = tmp_path / "run"
    branch = run / "branches/poisoned_night_safe"
    attempt = branch / "nights/01/proposals/evolution/attempt-safe"
    workspace = attempt / "workspace"
    workspace.mkdir(parents=True)
    original, prompt = "Native evolution instructions.\n", "Security defense."
    bootstrap = prompt + "\n\n" + original
    agents = workspace / "AGENTS.md"
    agents.write_text(bootstrap)
    source = tmp_path / "source/evolve_server/engines/agent_workspace.py"
    source.parent.mkdir(parents=True)
    source.write_text("_EVOLVE_AGENTS_MD = " + repr(original))
    from evolve_server.engines import agent_workspace
    monkeypatch.setattr(agent_workspace, "__file__", str(source))
    write(run / "config.runtime.json", {"source_worktree": str(tmp_path / "source"),
          "model_id": "local-model", "protocol_hash": "protocol"})
    write(run / "frozen-source.json", {"files": [{"path": str(source),
          "sha256": verifier._hash(source.read_bytes())}]})
    write(attempt.parent / "proposal-summary.json", {"attempt_dir": str(attempt)})
    write(workspace / "night-safe-prompt.json", {
        "sha256": verifier._hash(prompt.encode()), "bootstrap_path": str(agents),
        "original_bootstrap_sha256": verifier._hash(original.encode()),
        "defended_bootstrap_sha256": verifier._hash(bootstrap.encode()),
        "normal_evolution_instructions_preserved": True})
    write(attempt / "openclaw-home/.openclaw/openclaw.json", {
        "agents": {"defaults": {"workspace": str(workspace)}},
        "models": {"providers": {"sharedskill": {"baseUrl": "http://host/runs/night-safe/v1"}}}})
    request = run / "runtime/api-evidence/night-safe/request-1/request.json"
    write(request, {"model": "local-model", "tools": [{"type": "function"}], "messages": [
        {"role": "system", "content": "# Project Context\n## " + str(agents) + "\n" + bootstrap}]})
    record = request.parent / "record.json"
    write(record, {"request_id": "request-1", "run_id": "night-safe", "endpoint": "chat/completions",
        "role": "main", "actual_model": "local-model", "protocol_hash": "protocol",
        "status": "completed", "http_status": 200, "upstream_attempted": True})
    return run, branch, prompt, request, record


def test_exact_scoped_successful_system_delivery(proof):
    run, branch, prompt, _, _ = proof
    audit = verifier.verify_night_safe_prompt(run, branch, prompt)
    assert audit["verified"] is True
    assert audit["completed_request_count"] == 1
    assert audit["evidence"][0]["role"] == "system"


@pytest.mark.parametrize("fault", ["wrong_role", "wrong_run", "failed_response", "wrong_protocol",
                                 "other_branch_only", "summary_only", "bootstrap_modified"])
def test_unbound_or_unsuccessful_evidence_cannot_prove_delivery(proof, fault):
    run, branch, prompt, request, record = proof
    if fault in {"wrong_role", "summary_only"}:
        body = json.loads(request.read_text())
        if fault == "wrong_role":
            body["messages"][0]["role"] = "user"
        else:
            body["tools"] = []
        write(request, body)
    elif fault == "other_branch_only":
        other = run / "runtime/api-evidence/other-night/request-1"
        other.mkdir(parents=True)
        request.rename(other / request.name)
        record.rename(other / record.name)
    elif fault == "bootstrap_modified":
        agents = next(branch.rglob("AGENTS.md"))
        agents.write_text(agents.read_text() + "Changed normal instructions.")
    else:
        value = json.loads(record.read_text())
        value.update({"wrong_run": {"run_id": "other-night"},
                      "wrong_protocol": {"protocol_hash": "other-protocol"},
                      "failed_response": {"status": "error", "http_status": 502}}[fault])
        write(record, value)
    assert verifier.verify_night_safe_prompt(run, branch, prompt)["verified"] is False


def test_real_pilot_scoped_request_evidence_is_read_only():
    root = Path(__file__).resolve().parents[1]
    run = root / "experiment/reproduction/dymalskill-trajectory-e2e/runtime/runs/pilot-live-20261016-04"
    if not (run / "config.runtime.json").is_file():
        pytest.skip("Preserved real pilot evidence is not available")
    from scripts.injection.e2e import NIGHT_SAFE_PROMPT

    audit = verifier.verify_night_safe_prompt(run, run / "branches/poisoned_night_safe", NIGHT_SAFE_PROMPT)
    assert audit["verified"] is True, audit.get("error")
    assert audit["run_id"] == "night-4caf26e550b4de9e1b3e5d66"
    assert audit["completed_request_count"] == 9
    assert all(item["role"] == "system" for item in audit["evidence"])
