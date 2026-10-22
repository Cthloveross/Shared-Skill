from __future__ import annotations

import asyncio
import copy
import json
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from experiments.poisoned_evolution.spreadsheetbench import cli, continuation
from experiments.poisoned_evolution.spreadsheetbench.evolution import run_trial
from experiments.poisoned_evolution.spreadsheetbench.models import ProtocolError
from experiments.poisoned_evolution.spreadsheetbench.provenance import sha256_tree
from experiments.poisoned_evolution.tests.test_pe_spreadsheetbench_evolution import (
    FakeEngine,
    _batch,
    _config,
    _manifest,
    _write_s0,
)


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def source_run(tmp_path, monkeypatch):
    source_a, source_o = tmp_path / "old-a", tmp_path / "old-o"
    target_a, target_o = tmp_path / "new-a", tmp_path / "new-o"
    s0 = _write_s0(tmp_path)
    old = _config(tmp_path)
    old.update(protocol_version="workflow-canary-v2", run_ids={"formal": "old", "smoke": "old-smoke"})
    old["paths"] = {"usage_ledger": str(tmp_path / "usage.json")}
    old["budgets"].update(max_model_requests_total=4000, max_tokens_total=50_000_000)
    new = copy.deepcopy(old)
    new["run_ids"] = {"formal": "new", "smoke": "new-smoke"}
    new["budgets"].update(campaign_carryover_min_requests=64, campaign_carryover_min_tokens=1000)
    new["continuation"] = {"source_artifacts": str(source_a), "source_outputs": str(source_o)}
    _write(source_a / "frozen-config.json", old)
    _write(tmp_path / "usage.json", {"totals": {"requests": 64, "tokens_consumed": 1000, "tokens_reserved": 0}})
    manifests = [_manifest("batch-01"), _manifest("batch-02")]
    _write(source_a / "batch-manifests.json", {"batches": manifests})
    freeze = {"s0_bundle_sha256": sha256_tree(s0)}
    monkeypatch.setattr(cli, "_validate_source_freeze", lambda path: freeze)
    # The separately tested source-code validator is orthogonal to input import.
    monkeypatch.setattr(continuation, "validate_generation_code", lambda path: None)
    for index, manifest in enumerate(manifests):
        artifact = source_a / "trials/bf1" / manifest["batch_id"]
        output = source_o / "trials/bf1" / manifest["batch_id"]
        result = asyncio.run(run_trial(
            old, sessions=_batch(), manifest=manifest, family_id="bf1", artifact_dir=artifact,
            output_dir=output, s0_path=s0, engine=FakeEngine("bf1", mode="success" if index == 0 else "unknown"),
        ))
        if index == 0:
            # Simulate an old false negative without changing the generated skill.
            result = replace(result, ser_success=False)
        else:
            result = replace(result, engine=replace(
                result.engine, error_type="InvalidEvolverOutput", infrastructure_signature="old-model-error",
            ))
        _write(output / "trial-result.json", result.to_dict())
        inputs = json.loads((artifact / "transformed-sessions.json").read_text())
        _write(artifact / "engine/request.json", {"sessions": inputs})
        for name, text in result.engine.materialized_skills.items():
            path = artifact / "engine/storage/pe-single-round/skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for session_id, text in result.engine.summaries.items():
            path = output / "engine/debug/summarizer" / f"{session_id}_summary.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    _write(source_o / "matrix-state.json", {"status": "stopped", "started_unix": 100, "elapsed_seconds": 681})
    _write(source_o / "matrix-result.json", {"completed": 1, "unknown": 1, "attempted": 2})
    return new, dict(artifact_root=target_a, output_root=target_o, manifests=manifests, corpus_sessions=_batch(), s0_path=s0)


def test_import_rescores_without_generation_preserves_unknown_and_source(source_run):
    config, arguments = source_run
    source_a = Path(config["continuation"]["source_artifacts"])
    source_o = Path(config["continuation"]["source_outputs"])
    before = sha256_tree(source_a), sha256_tree(source_o)
    record = continuation.prepare_continuation(config, **arguments)
    assert record["counts"] == {"completed": 1, "unknown": 1}
    assert record["attempts"][0]["source_ser_success"] is False
    assert record["attempts"][0]["rescored_ser_success"] is True
    assert record["attempts"][1]["model_error_reclassified"] is True
    imported = json.loads((arguments["output_root"] / "trials/bf1/batch-02/trial-result.json").read_text())
    assert imported["status"] == "unknown" and imported["ser_success"] is None
    assert imported["engine"]["error_type"] == "InvalidEvolverOutput"
    assert imported["engine"]["infrastructure_signature"] is None
    assert before == (sha256_tree(source_a), sha256_tree(source_o))
    assert not (arguments["artifact_root"] / "smoke-gate.json").exists()
    state = json.loads((arguments["output_root"] / "matrix-state.json").read_text())
    assert state["status"] == "imported" and state["elapsed_seconds"] == 681
    assert continuation.prepare_continuation(config, **arguments) == record


@pytest.mark.parametrize("field", ["model", "engine", "matrix", "budgets"])
def test_config_changes_fail_closed(source_run, field):
    config, arguments = source_run
    changed = copy.deepcopy(config)
    changed[field]["unregistered_change"] = True
    with pytest.raises(ProtocolError, match="changes generation config"):
        continuation.prepare_continuation(changed, **arguments)
    assert not arguments["output_root"].exists()


def test_changed_input_rejected_before_import(source_run):
    config, arguments = source_run
    arguments["corpus_sessions"][0]["turns"][0]["prompt_text"] = "different task"
    with pytest.raises(ProtocolError, match="changes actual trial input"):
        continuation.prepare_continuation(config, **arguments)
    assert not arguments["output_root"].exists()


def test_budget_reset_rejected(source_run):
    config, arguments = source_run
    config["budgets"]["campaign_carryover_min_requests"] = 100
    with pytest.raises(ProtocolError, match="usage floor"):
        continuation.prepare_continuation(config, **arguments)
    assert not arguments["output_root"].exists()


def test_imported_result_tampering_rejected(source_run):
    config, arguments = source_run
    continuation.prepare_continuation(config, **arguments)
    result = arguments["output_root"] / "trials/bf1/batch-01/trial-result.json"
    result.write_text("{}")
    with pytest.raises(ProtocolError, match="imported trial result changed"):
        continuation.prepare_continuation(config, **arguments)


def test_interrupted_import_can_finish_without_replacing_source_or_elapsed(source_run):
    config, arguments = source_run
    record = continuation.prepare_continuation(config, **arguments)
    (arguments["artifact_root"] / "continuation-record.json").unlink()
    state_path = arguments["output_root"] / "matrix-state.json"
    state = json.loads(state_path.read_text())
    state["elapsed_seconds"] = 700
    _write(state_path, state)
    assert continuation.prepare_continuation(config, **arguments) == record
    assert json.loads(state_path.read_text())["elapsed_seconds"] == 700


def test_generation_code_checks_frozen_request_and_transform_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with tarfile.open(source / "poisoned-evolution-spreadsheetbench-driver.tar.gz", "w:gz") as archive:
        for name in ("transforms.py", "corpus.py", "corpus_import.py", "engine.py", "_e9c_bridge.py"):
            archive.add(continuation.DRIVER_ROOT / name, arcname=f"driver/{name}")
    continuation.validate_generation_code(tmp_path)
    old = "async def _run(x):\n import a\n summary = await server.run_once()\n return 1\n"
    new = "async def _run(x):\n import b\n summary = await server.run_once()\n return 2\n"
    assert continuation._definition(old, "_run", through_call="run_once") == continuation._definition(
        new, "_run", through_call="run_once",
    )
    changed = old.replace("run_once()", "run_once(temperature=2)")
    assert continuation._definition(old, "_run", through_call="run_once") != continuation._definition(
        changed, "_run", through_call="run_once",
    )
