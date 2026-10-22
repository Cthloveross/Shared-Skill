"""Exercise current method corrections, or their unapplied review artifact."""

from __future__ import annotations

import ast
import copy
import shutil
import subprocess
import threading
import types
from pathlib import Path

import pytest

from scripts.injection import e2e_30 as cohort
from scripts.injection import e2e_cycles as cycles


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def proposal(tmp_path):
    """Use applied functions directly; patch only throwaway legacy copies."""
    paths = {name: ROOT / "scripts/injection" / name for name in ("e2e_30.py", "e2e_cycles.py")}
    sources = {path: path.read_bytes() for path in paths.values()}
    definitions = {
        name: {node.name: node for node in ast.parse(sources[path]).body if isinstance(node, ast.FunctionDef)}
        for name, path in paths.items()
    }
    grouped = any(isinstance(node, ast.Name) and node.id == "assignments"
                  for node in ast.walk(definitions["e2e_30.py"]["run_day_cohort"]))
    assert grouped, "The maintained Day scheduler must group tasks explicitly"
    source_root = ROOT

    def load(original, name, selected):
        path = source_root / "scripts/injection" / name
        tree = ast.parse(path.read_text())
        definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in selected]
        assert {node.name for node in definitions} == set(selected)
        module = types.ModuleType("isolated_method_correction")
        module.__dict__.update(original.__dict__)
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), module.__dict__)
        return module

    day = load(cohort, "e2e_30.py", ["run_day_cohort"])
    night = load(cycles, "e2e_cycles.py", ["night_inputs", "run_native_night"])
    yield day, night
    for source, original in sources.items():
        assert source.read_bytes() == original, "An active frozen source was modified"


def test_one_user_is_sequential_in_input_order_and_distinct_users_overlap(tmp_path, proposal, monkeypatch):
    day, _ = proposal
    tasks = [{"short_id": f"task-{index:02d}", "user_id": f"user-{index % 8 + 1:02d}"}
             for index in range(30)]
    expected = {user: [task["short_id"] for task in tasks if task["user_id"] == user]
                for user in {task["user_id"] for task in tasks}}
    actual = {user: [] for user in expected}
    active = set()
    maximum_parallel = 0
    lock = threading.Lock()
    overlap = threading.Barrier(2)
    progress = []
    original_write = day.e2e.write_json

    def record(path, value):
        if path.name == "progress.json":
            progress.append(value["completed_or_failed"])
        original_write(path, value)

    def execute(_run, _settings, task, *_args):
        nonlocal maximum_parallel
        user = task["user_id"]
        with lock:
            assert user not in active, f"The same simulated user overlapped: {user}"
            active.add(user)
            actual[user].append(task["short_id"])
            maximum_parallel = max(maximum_parallel, len(active))
        if task["short_id"] in {"task-00", "task-01"}:
            overlap.wait(timeout=5)
        with lock:
            active.remove(user)
        return ({"status": "completed", "task_score": 0.5}, {"session_id": task["short_id"]})

    monkeypatch.setattr(day, "run_day_task", execute)
    monkeypatch.setattr(day.e2e, "write_json", record)
    states, sessions = day.run_day_cohort(tmp_path, {}, tasks, None, "unused", None)
    assert actual == expected
    assert maximum_parallel >= 2
    assert progress == list(range(1, 31)), "Progress must update per task, including within user groups"
    assert len(states) == len(sessions) == 30
    assert day.e2e.read_json(tmp_path / "day/state.json")["status"] == "completed"


def test_unknown_user_is_rejected_before_any_agent_call(tmp_path, proposal, monkeypatch):
    day, _ = proposal
    monkeypatch.setattr(day, "run_day_task", lambda *args: pytest.fail("Unknown user must not execute"))
    with pytest.raises(ValueError, match="unknown simulated user"):
        day.run_day_cohort(tmp_path, {}, [{"short_id": "01.01", "user_id": "other"}], None, "unused", None)


@pytest.mark.parametrize("number", [2, 6])
def test_actual_round_reaches_native_normalized_aggregate_without_rewriting_input(tmp_path, proposal, number):
    _, night = proposal
    _settings, _engine, _environment, evolution, *_ = night.e2e.load_components()
    original = {
        "session_id": "real-upload", "original_session_id": "real-upload", "task_id": "task-a",
        "native_uploaded_sha256": "raw-upload-sha", "round": 1, "phase": "day",
        "aggregate": {"round": 1}, "outcome": {"score": 0.5},
        "turns": [{"prompt_text": "Actual task", "response_text": "Actual response", "read_skills": []}],
    }
    original_copy = copy.deepcopy(original)
    uploaded = tmp_path / "uploaded.json"
    night.e2e.write_json(uploaded, original)
    uploaded_bytes = uploaded.read_bytes()
    sessions, manifest = night.night_inputs(tmp_path, "clean", [{"task_id": "task-a"}], [original], {},
                                          number=number)
    normalized = evolution._normalize_session(sessions[0], set(), restrict_to_bank=False)
    assert normalized["round"] == normalized["aggregate"]["round"] == number
    assert manifest["round"] == number
    assert original == original_copy
    assert uploaded.read_bytes() == uploaded_bytes
    evidence = night.e2e.read_json(tmp_path / "inputs/evidence/real-upload.json")
    assert evidence["tampered"] is False
    assert evidence["round_metadata"] == {
        "source_round": 1, "cycle_round": number, "original_uploaded_object_unchanged": True,
    }


@pytest.mark.parametrize("number", [2, 6])
@pytest.mark.parametrize("started", [False, True])
def test_actual_controller_publishes_correct_number_without_generating_another_proposal(
    tmp_path, proposal, monkeypatch, number, started,
):
    _, night = proposal
    _base, _engine, _environment, evolution, controller, _runtime, _align, tree_hash, writer = (
        night.e2e.load_components()
    )
    bank = tmp_path / "banks/S0/alpha"
    bank.mkdir(parents=True)
    (bank / "SKILL.md").write_text("---\nname: alpha\ndescription: Existing skill\n---\nPreserved parent.\n")
    branch = tmp_path / "branches/clean"
    cached = branch / "nights" / f"{number:02d}" / "candidates.json"
    night.e2e.write_json(cached, [])
    cached_bytes = cached.read_bytes()
    if started:
        night.e2e.write_json(branch / "night-state.json", {"status": "started", "round": number})
    settings = {"all_tasks": [], "cycle_round": number, "rounds": 6, "protocol_hash": "isolated-protocol",
                "alignment": {"version": "native-night-atomic-v1"}}
    monkeypatch.setattr(evolution, "propose_candidates", lambda *args: pytest.fail("Finished proposals are reused"))
    result = night.run_native_night(tmp_path, settings, [], "clean", controller, evolution, tree_hash, writer)
    expected = branch / "banks" / f"S{number}"
    assert result["published_bank"] == str(expected)
    assert result["round"] == result["publication"]["round"] == number
    assert result["publication"]["changeset_accepted"] is False
    assert result["validation_recovered_without_repeating_evolution"] is True
    assert cached.read_bytes() == cached_bytes
    assert tree_hash(expected) == tree_hash(tmp_path / "banks/S0")
    assert not (branch / "nights/01/publication.json").exists()


def test_cached_legacy_night1_is_reused_without_rewriting_its_results(tmp_path, proposal, monkeypatch):
    _, night = proposal
    branch = tmp_path / "branches/clean"
    published = branch / "banks/S1"
    published.mkdir(parents=True)
    publication = {"round": 1, "bank_sha256": "unchanged", "changeset_accepted": False}
    legacy = {"status": "completed", "published_bank": str(published),
              "published_bank_sha256": "unchanged", "publication": publication}
    marker = branch / "night-state.json"
    publication_path = branch / "nights/01/publication.json"
    night.e2e.write_json(marker, legacy)
    night.e2e.write_json(publication_path, publication)
    saved = {path: path.read_bytes() for path in (marker, publication_path)}
    monkeypatch.setattr(night.e2e, "write_json",
                        lambda *args: pytest.fail("Legacy evidence must remain byte-identical"))
    result = night.run_native_night(tmp_path, {"cycle_round": 1}, [], "clean", None, None,
                                    lambda _path: "unchanged", None)
    assert result == legacy
    assert all(path.read_bytes() == raw for path, raw in saved.items())


def test_partial_later_night_without_candidate_cache_does_not_repeat_evolution(tmp_path, proposal):
    _, night = proposal
    night.e2e.write_json(tmp_path / "branches/clean/night-state.json", {"status": "started", "round": 4})
    with pytest.raises(RuntimeError, match="refusing a duplicate call"):
        night.run_native_night(tmp_path, {"cycle_round": 4, "rounds": 6}, [], "clean", None, None, None, None)


@pytest.mark.parametrize("number", [0, 7, True])
def test_native_round_outside_configured_cycles_is_rejected(tmp_path, proposal, number):
    _, night = proposal
    with pytest.raises(ValueError, match="configured round"):
        night.run_native_night(tmp_path, {"cycle_round": number, "rounds": 6}, [], "clean", None, None, None, None)
