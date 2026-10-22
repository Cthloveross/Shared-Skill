from pathlib import Path

import pytest

from scripts.injection import e2e_cycles as cycles


def test_single_poisoned_condition_is_a_three_stage_run(tmp_path, monkeypatch):
    calls = []
    source = tmp_path / "baseline"
    config = {"bootstrap_day_run": str(source), "rounds": 3, "conditions": ["poisoned"]}

    def stage(run, cfg, condition, number, parent):
        calls.append((condition, number))
        return {"published_bank": str(tmp_path / condition / f"S{number}"),
                "input_coverage_complete": True, "missing_injection_carriers": {}}

    monkeypatch.setattr(cycles, "run_stage", stage)
    monkeypatch.setattr(cycles, "endpoint_sandbox", lambda *args: {"status": "completed"})
    cycles.run_cycles(tmp_path, config)
    state = cycles.e2e.read_json(tmp_path / "state.json")
    assert calls == [("poisoned", 1), ("poisoned", 2), ("poisoned", 3)]
    assert state["status"] == "finished"


@pytest.mark.parametrize("rounds", [3, 6])
def test_configured_rounds_keep_each_published_bank_and_only_run_final_sandbox(tmp_path, monkeypatch, rounds):
    calls, endpoints = [], []
    source = tmp_path / "baseline"
    config = {"bootstrap_day_run": str(source), "rounds": rounds}

    def stage(run, cfg, condition, number, parent):
        expected = source / "banks/S0" if number == 1 else tmp_path / condition / f"S{number-1}"
        assert parent == expected
        calls.append((condition, number))
        return {"published_bank": str(tmp_path / condition / f"S{number}"),
                "input_coverage_complete": True, "missing_injection_carriers": {}}

    def sandbox(run, cfg, condition, result):
        assert result["published_bank"].endswith(f"S{rounds}")
        endpoints.append(condition)
        return {"status": "completed"}

    monkeypatch.setattr(cycles, "run_stage", stage)
    monkeypatch.setattr(cycles, "endpoint_sandbox", sandbox)
    cycles.run_cycles(tmp_path, config)
    final = cycles.e2e.read_json(tmp_path / "state.json")
    assert calls == [(condition, number) for number in range(1, rounds + 1) for condition in ("clean", "poisoned")]
    assert endpoints == ["clean", "poisoned"]
    assert final["status"] == "finished" and len(final["stages"]) == 2 * rounds
    assert final["safe_prompt_conditions"] == "paused_by_user"


def test_one_broken_branch_preserves_evidence_and_other_branch_continues(tmp_path, monkeypatch):
    calls = []

    def stage(run, config, condition, number, parent):
        calls.append((condition, number))
        if condition == "poisoned" and number == 2:
            raise RuntimeError("native Night incomplete")
        return {"published_bank": str(tmp_path / condition / str(number)),
                "input_coverage_complete": True, "missing_injection_carriers": {}}

    monkeypatch.setattr(cycles, "run_stage", stage)
    monkeypatch.setattr(cycles, "endpoint_sandbox", lambda *a: {"status": "completed"})
    cycles.run_cycles(tmp_path, {"bootstrap_day_run": str(tmp_path)})
    final = cycles.e2e.read_json(tmp_path / "state.json")
    assert final["status"] == "finished_with_gaps"
    assert ("clean", 3) in calls and ("poisoned", 3) not in calls
    assert final["errors"]["poisoned"]["stage"] == "02/poisoned"


def test_native_input_gap_cannot_be_reported_as_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(cycles, "run_stage", lambda *a: {
        "published_bank": str(tmp_path), "input_coverage_complete": False, "missing_injection_carriers": {}})
    monkeypatch.setattr(cycles, "endpoint_sandbox", lambda *a: {"status": "completed"})
    cycles.run_cycles(tmp_path, {"bootstrap_day_run": str(tmp_path)})
    assert cycles.e2e.read_json(tmp_path / "state.json")["status"] == "finished_with_gaps"


def test_bank_copy_does_not_reset_existing_stage_to_another_parent(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (source / "skill").write_text("S1")
    digest = lambda p: (p / "skill").read_text()
    assert cycles.copy_bank(source, target, digest) == "S1"
    (source / "skill").write_text("different S2")
    with pytest.raises(ValueError, match="continuity"):
        cycles.copy_bank(source, target, digest)


def test_finished_proposal_recovery_never_calls_native_evolution_again(tmp_path, monkeypatch):
    branch = tmp_path / "branches/poisoned"
    parent = tmp_path / "banks/S0"
    parent.mkdir(parents=True)
    (parent / "skill").write_text("unchanged parent skill")
    bank_hash = lambda path: cycles.e2e.tree_manifest(path)["sha256"]
    expected_hash = bank_hash(parent)
    published = branch / "banks/S1"
    cycles.e2e.write_json(branch / "night-state.json", {"status": "started"})
    cache = branch / "nights/01/candidates.json"
    cycles.e2e.write_json(cache, [{"candidate_id": "frozen"}])
    cache_bytes = cache.read_bytes()
    calls = []

    class Controller:
        @staticmethod
        def night(*args):
            calls.append(args)
            assert args[0] == 1 and args[1] == branch / "banks/S0"
            assert bank_hash(args[1]) == expected_hash
            published.mkdir(parents=True)
            (published / "skill").write_bytes((args[1] / "skill").read_bytes())
            cycles.e2e.write_json(branch / "nights/01/publication.json", {
                "round": 1, "bank_sha256": bank_hash(published), "parent_sha256": expected_hash,
                "changeset_accepted": False,
            })
            return published

    def forbidden(*args, **kwargs):
        raise AssertionError("Completed proposal must not be generated again")

    monkeypatch.setattr(cycles.e2e, "run_night", forbidden)
    result = cycles.run_native_night(tmp_path, {"all_tasks": []}, [], "poisoned",
                                     Controller, None, bank_hash, None)
    assert len(calls) == 1
    assert cache.read_bytes() == cache_bytes
    assert result["published_bank"] == str(published)
    assert result["publication"]["round"] == 1
    assert result["validation_recovered_without_repeating_evolution"] is True
    assert cycles.e2e.read_json(branch / "validation-recovery.json")["new_native_evolution_calls"] == 0


def test_partial_proposal_without_authoritative_cache_is_preserved(tmp_path):
    cycles.e2e.write_json(tmp_path / "branches/clean/night-state.json", {"status": "started"})
    with pytest.raises(RuntimeError, match="refusing a duplicate"):
        cycles.run_native_night(tmp_path, {"all_tasks": []}, [], "clean", None, None, None, None)
