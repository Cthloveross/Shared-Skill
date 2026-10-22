from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys

import pytest

from scripts.injection import cycle_revision as revision


@pytest.fixture
def original(tmp_path, monkeypatch):
    e2e = revision.e2e
    components = e2e.load_components()
    evolution, tree_hash = components[3], components[-2]
    monkeypatch.setattr(revision.cohort, "validate_resume", lambda *args: None)
    run = tmp_path / "original-run"
    run.mkdir()
    source_run = tmp_path / "original-day"
    e2e.write_json(source_run / "config.runtime.json", {"all_tasks": []})
    e2e.write_json(source_run / "frozen-tasks.json", [])
    bootstrap = tmp_path / "bootstrap"
    e2e.write_json(bootstrap / "finalized-day-inputs/manifest.json", {"task_count": 30})
    payload = run / "frozen-payload.json"
    payload.write_text('{"text":"disposable canary"}')
    old_code = tmp_path / "old-orchestrator.py"
    old_code.write_text("# preserved original code\n")
    new_code = tmp_path / "new-orchestrator.py"
    new_code.write_text("# explicitly versioned correction\n")
    old_sources = [{"path": str(old_code), "sha256": e2e.sha256(old_code.read_bytes())}]
    sources = [{"path": str(new_code), "sha256": e2e.sha256(new_code.read_bytes())}]
    snapshot = run / "frozen-orchestrator/00-old-orchestrator.py"
    snapshot.parent.mkdir()
    snapshot.write_bytes(old_code.read_bytes())
    settings = {"protocol_hash": "old-execution-protocol", "alignment": {
        "version": "native-night-atomic-v1", "source_policy": "native-history-explicit-day-sources-v1"}}
    original_config = {"run_id": run.name, "source_run": str(source_run), "bootstrap_day_run": str(bootstrap),
        "bootstrap_manifest_sha256": e2e.sha256((bootstrap / "finalized-day-inputs/manifest.json").read_bytes()),
        "frozen_payload": str(payload), "payload_sha256": e2e.sha256(payload.read_bytes()),
        "conditions": ["clean", "poisoned"], "sources": old_sources, "settings": settings}
    e2e.write_json(run / "cycles-config.json", original_config)
    bank = tmp_path / "parent-bank/skill"
    bank.mkdir(parents=True)
    bank.joinpath("SKILL.md").write_text("---\nname: skill\ndescription: Original skill\n---\nOriginal body.\n")
    candidates = {}
    for condition in original_config["conditions"]:
        stage = run / "rounds/01" / condition
        branch = stage / "branches" / condition
        attempt = branch / "nights/01/proposals/evolution/attempt-original"
        bundle = attempt / "candidate/skill"
        bundle.joinpath("history").mkdir(parents=True)
        bundle.joinpath("SKILL.md").write_text("---\nname: skill\ndescription: Improved skill\n---\nImproved body.\n")
        bundle.joinpath("history/v1_evidence.md").write_text(
            "Source session `real-day-session`; task 02_Code_Intelligence_task_7_connect_the_dots")
        from sharedskill.skill_bundle import read_skill_bundle_with_meta, bundle_file_records

        raw, _, native_hash = read_skill_bundle_with_meta(bundle)
        candidate = {"name": "native-night-changeset", "action": "atomic_changeset",
            "candidate_id": "historical-" + condition, "source_bank_sha256": tree_hash(bank.parent),
            "source_task_ids": [], "source_session_ids": [], "source_evidence": [],
            "reason": "unverifiable_sources", "source_error": "historical strict rejection",
            "bundles": [{"name": "skill", "action": "refine", "bundle_dir": str(bundle),
                "bundle_sha256": tree_hash(bundle), "native_tree_sha256": native_hash,
                "files": bundle_file_records(raw)}]}
        candidates[condition] = candidate
        e2e.write_json(attempt.parent / "proposal-summary.json", {"status": "completed",
            "source_bank_sha256": tree_hash(bank.parent), "candidates": [], "quarantines": [candidate]})
        e2e.write_json(branch / "nights/01/candidates.json", [])
        e2e.write_json(branch / "nights/01/publication.json", {"changeset_accepted": False})
        e2e.write_json(stage / "inputs/sessions/day.json", {"session_id": "real-day-session",
            "task_id": "02_Code_Intelligence_task_7_connect_the_dots_medium_img_zh", "phase": "day",
            "turns": [{"turn_num": 1, "tool_calls": [], "response_text": "done"}],
            "skill_bank_sha256": tree_hash(bank.parent), "outcome": {"score": 0.25}})
    return {"run": run, "sources": sources, "evolution": evolution, "tree_hash": tree_hash,
            "bank": bank.parent, "candidates": candidates, "config": original_config}


def snapshot_tree(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
            and "revisions" not in path.relative_to(root).parts}


def seed(original, config, condition="clean"):
    stage = Path(config["stage_root"]) / "rounds/01" / condition
    stage_bank = stage / "banks/S0"
    import shutil

    if not stage_bank.exists():
        shutil.copytree(original["bank"], stage_bank)
    settings = copy.deepcopy(config["settings"])
    settings["protocol_hash"] = "new-stage-protocol-" + condition
    revision.seed_night1(stage, settings, config, condition, original["evolution"], original["tree_hash"])
    return stage, settings


def test_revision_revalidates_exact_original_bytes_without_native_call(original, monkeypatch):
    before = snapshot_tree(original["run"])
    monkeypatch.setattr(original["evolution"], "propose_candidates", lambda *args: pytest.fail("Repeated native evolution"))
    config = revision.prepare(original["run"], original["sources"])
    stage, settings = seed(original, config)
    cached = revision.e2e.read_json(stage / "branches/clean/nights/01/candidates.json")
    assert len(cached) == 1
    assert cached[0]["candidate_id"] != original["candidates"]["clean"]["candidate_id"]
    assert cached[0]["bundles"] == original["candidates"]["clean"]["bundles"]
    assert cached[0]["source_task_ids"] == ["02_Code_Intelligence_task_7_connect_the_dots_medium_img_zh"]
    assert "reason" not in cached[0] and "source_error" not in cached[0]
    assert settings["protocol_hash"] != original["config"]["settings"]["protocol_hash"]
    assert config["settings"]["alignment"]["source_policy"] == "native-history-explicit-day-sources-v1"
    assert config["settings"]["alignment"]["source_evidence_revision"] == revision.POLICY
    assert snapshot_tree(original["run"]) == before
    assert revision.e2e.read_json(stage / "native-proposal-import.json")["new_native_calls"] == 0
    controller = revision.e2e.load_components()[4]
    validated = []

    def gate(number, bank, sessions, tasks, run_dir, config, directory, candidates):
        validated.extend(candidates)
        return bank

    monkeypatch.setattr(controller, "_alignment_night", gate)
    controller.night(1, stage / "banks/S0", [], [], stage / "branches/clean", settings)
    assert validated == cached  # Native controller consumes the imported cache instead of proposing again.
    assert revision.prepare(original["run"], original["sources"]) == config
    seed(original, config)  # Identical candidate cache reuse is idempotent.
    inputs = [revision.e2e.read_json(path) for path in
              (original["run"] / "rounds/01/clean/inputs/sessions").glob("*.json")]
    revision.verify_night1_inputs(stage, inputs)
    inputs[0]["turns"][0]["response_text"] = "changed input"
    with pytest.raises(ValueError, match="inputs differ"):
        revision.verify_night1_inputs(stage, inputs)


def test_completed_proposal_without_publication_preserves_failure_evidence(original):
    branch = original["run"] / "rounds/01/poisoned/branches/poisoned"
    (branch / "nights/01/publication.json").unlink()
    error = branch.parent.parent / "cycle-error.json"
    revision.e2e.write_json(error, {"error": "Missing old provenance manifest during A/B"})
    before = error.read_bytes()
    config = revision.prepare(original["run"], original["sources"])
    origin = config["native_night1_origins"]["poisoned"]
    assert origin["original_publication_present"] is False
    assert any(item["path"] == str(error) for item in origin["artifacts"])
    stage, _ = seed(original, config, "poisoned")
    assert len(revision.e2e.read_json(stage / "branches/poisoned/nights/01/candidates.json")) == 1
    assert error.read_bytes() == before and not (branch / "nights/01/publication.json").exists()


def test_unknown_sources_stay_quarantined_without_promoting_source_labels(original):
    candidate = original["candidates"]["clean"]
    bundle = Path(candidate["bundles"][0]["bundle_dir"])
    (bundle / "history/v1_evidence.md").write_text("02_Code_Intelligence_task_7_connect_the_dots")
    from sharedskill.skill_bundle import read_skill_bundle_with_meta, bundle_file_records

    raw, _, native_hash = read_skill_bundle_with_meta(bundle)
    candidate["bundles"][0].update(bundle_sha256=original["tree_hash"](bundle),
        native_tree_sha256=native_hash, files=bundle_file_records(raw))
    summary = bundle.parents[2] / "proposal-summary.json"
    revision.e2e.write_json(summary, {"status": "completed", "source_bank_sha256": original["tree_hash"](original["bank"]),
        "candidates": [], "quarantines": [candidate]})
    config = revision.prepare(original["run"], original["sources"])
    stage, _ = seed(original, config)
    night = stage / "branches/clean/nights/01"
    assert revision.e2e.read_json(night / "candidates.json") == []
    quarantines = revision.e2e.read_json(night / "proposals/evolution/proposal-summary.json")["quarantines"]
    assert len(quarantines) == 1 and quarantines[0]["source_task_ids"] == []
    assert "no changed native history" in quarantines[0]["source_error"]


@pytest.mark.parametrize("fault", ["original_summary", "candidate_bytes", "payload", "bootstrap", "source_snapshot"])
def test_reseed_rejects_changed_original_or_revision_sources(original, fault):
    config = revision.prepare(original["run"], original["sources"])
    if fault == "original_summary":
        path = Path(config["native_night1_origins"]["clean"]["branch"]) / "nights/01/proposals/evolution/proposal-summary.json"
    elif fault == "candidate_bytes":
        path = Path(original["candidates"]["clean"]["bundles"][0]["bundle_dir"]) / "SKILL.md"
    elif fault == "payload":
        path = Path(original["config"]["frozen_payload"])
    elif fault == "bootstrap":
        path = Path(original["config"]["bootstrap_day_run"]) / "finalized-day-inputs/manifest.json"
    else:
        path = next((Path(config["stage_root"]) / "frozen-source").glob("*"))
    path.write_bytes(path.read_bytes() + b"\nchanged")
    with pytest.raises(ValueError):
        seed(original, config)


def test_incomplete_native_proposal_cannot_be_seeded(original):
    path = original["run"] / "rounds/01/clean/branches/clean/nights/01/proposals/evolution/proposal-summary.json"
    value = revision.e2e.read_json(path)
    value["status"] = "running"
    revision.e2e.write_json(path, value)
    with pytest.raises(ValueError, match="native proposals must finish"):
        revision.prepare(original["run"], original["sources"])


def test_execution_context_scopes_both_adapters_and_restores_on_failure(original, monkeypatch):
    config = revision.prepare(original["run"], original["sources"])
    engine = original["evolution"]
    native = engine._history_sources
    from types import SimpleNamespace
    from scripts.injection import cycle_validation

    controller = SimpleNamespace(_history_sources=native)
    entries = []

    @contextmanager
    def validation_adapter(selected_controller, binding):
        entries.append((selected_controller, binding))
        yield

    monkeypatch.setattr(cycle_validation, "validation_adapter", validation_adapter)
    with pytest.raises(RuntimeError, match="simulated failure"):
        with revision.execution_context(config, controller, engine):
            assert engine._history_sources is controller._history_sources
            assert engine._history_sources.source_policy == revision.POLICY
            raise RuntimeError("simulated failure")
    assert engine._history_sources is native and controller._history_sources is native
    assert len(entries) == 1
