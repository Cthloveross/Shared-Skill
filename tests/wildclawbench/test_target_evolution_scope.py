from __future__ import annotations

import json
import subprocess

import pytest

from scripts.wildclawbench import evolution, protocol


def test_target_only_prompt_forbids_create_and_does_not_claim_ab():
    prompt = evolution._prompt(
        "artifact-finalization",
        ["task-1"],
        target_only=True,
        controller_ab=False,
    )
    assert "refine the allowed existing skill, OR skip" in prompt
    assert "Do not create a new skill" in prompt
    assert "For create" not in prompt
    assert "### create_skill" not in prompt
    assert '"new_skill_creation_allowed": false' in prompt
    assert '"action": "refine"' in prompt
    assert "edit only the allowed existing skill's SKILL.md" in prompt
    assert "Do not add, delete, rename, or modify any supporting or history file" in prompt
    assert "directly publish the exact proposal without a task A/B rerun" in prompt
    assert "will run the real toolchain A/B test" not in prompt


def _session(index: int, observed: list[str] | None = None) -> dict:
    return {
        "task_id": f"task-{index:02d}",
        "source_session_id": f"session-{index:02d}",
        "_skills_referenced": list(observed or []),
    }


def _config(expected: int = 30) -> dict:
    return {
        "target_evolution_scope": {
            "version": evolution.TARGET_SCOPE_VERSION,
            "skill_name": "artifact-finalization",
            "expected_sessions": expected,
        }
    }


def test_target_scope_routes_exactly_one_logical_record_per_task_without_faking_reads():
    sessions = [_session(index, ["artifact-finalization"] if index < 8 else ["other"])
                for index in range(30)]

    report = evolution._apply_target_evolution_scope(
        sessions, {"artifact-finalization", "other"}, _config()
    )

    assert report["logical_record_count"] == report["unique_task_count"] == 30
    assert report["actual_target_read_count"] == 8
    assert report["routing_is_not_claimed_as_an_observed_read"] is True
    assert all(row["_skills_referenced"] == ["artifact-finalization"] for row in sessions)
    assert sessions[8]["_observed_skills_referenced"] == ["other"]
    assert sessions[8]["target_evolution_routing"]["actual_target_read_observed"] is False


@pytest.mark.parametrize("mutation, message", [
    (lambda rows: rows.pop(), "expected 30"),
    (lambda rows: rows.__setitem__(1, {**rows[1], "task_id": rows[0]["task_id"]}), "unique session"),
])
def test_target_scope_rejects_wrong_n_or_duplicate_tasks(mutation, message):
    sessions = [_session(index) for index in range(30)]
    mutation(sessions)
    with pytest.raises(evolution.EvolutionError, match=message):
        evolution._apply_target_evolution_scope(sessions, {"artifact-finalization"}, _config())


def test_target_scope_requires_existing_target_and_cannot_hide_inside_alignment():
    sessions = [_session(index) for index in range(3)]
    with pytest.raises(evolution.EvolutionError, match="absent"):
        evolution._apply_target_evolution_scope(sessions, {"other"}, _config(3))
    with pytest.raises(evolution.EvolutionError, match="whole-night alignment"):
        evolution._apply_target_evolution_scope(
            sessions,
            {"artifact-finalization"},
            {**_config(3), "alignment": {"version": "native-night-atomic-v1"}},
        )


def test_longitudinal_text_endpoint_counts_only_three_days():
    tasks = [{"task_id": str(index)} for index in range(30)]
    assert protocol.fixed_execution_count(tasks, {
        "execution_plan": "longitudinal-day-night-text-endpoint-v1", "rounds": 3
    }) == 90


def test_native_proposal_receives_one_target_group_with_all_thirty_tasks(tmp_path, monkeypatch):
    bank = tmp_path / "bank"
    for name in ("artifact-finalization", "other"):
        directory = bank / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: fixture\n---\nOriginal {name} procedure.\n"
        )
    bank_hash = evolution._tree_hash(bank)
    sessions = [{
        "task_id": f"task-{index:02d}",
        "session_id": f"session-{index:02d}",
        "phase": "day",
        "skill_bank_sha256": bank_hash,
        "outcome": {"score": 1.0},
        "turns": [{
            "prompt_text": "produce artifact",
            "response_text": "done",
            "read_skills": [{"skill_name": "other"}],
            "tool_errors": [],
        }],
    } for index in range(30)]

    class Client:
        def __init__(self, **kwargs):
            pass

        async def chat(self, messages, **kwargs):
            return "The task completed after local output validation."

    calls = []

    def runner(workspace, home, message, config, session_id):
        scope_text = (workspace / "EVOLVE_AGENTS.md").read_text().split(
            "# Independent experimental proposal scope\n\n", 1
        )[1]
        scope, _ = json.JSONDecoder().raw_decode(scope_text)
        calls.append(scope)
        target = workspace / "skills/artifact-finalization/SKILL.md"
        target.write_text(
            "---\nname: artifact-finalization\ndescription: fixture\n---\nImproved local validation.\n"
        )
        return subprocess.CompletedProcess([], 0, "ok", "")

    monkeypatch.setattr(evolution, "AsyncLLMClient", Client)
    candidates = evolution.propose_candidates(tmp_path / "night", bank, sessions, {
        "llm_model": "local",
        "llm_base_url": "http://127.0.0.1:18181/v1",
        "llm_max_tokens": 64,
        "llm_temperature": 0,
        "paid_api_budget_usd": 0,
        "evolution_runner": runner,
        **_config(),
    })

    assert len(calls) == len(candidates) == 1
    assert calls[0]["allowed_existing_skill_edit"] == "artifact-finalization"
    assert len(calls[0]["source_task_ids"]) == 30
    assert candidates[0]["name"] == "artifact-finalization"
    assert len(candidates[0]["source_task_ids"]) == len(candidates[0]["source_session_ids"]) == 30
    summary = json.loads((tmp_path / "night/evolution/proposal-summary.json").read_text())
    assert summary["target_evolution_scope"]["logical_record_count"] == 30
    assert summary["target_evolution_scope"]["actual_target_read_count"] == 0
    assert summary["groups"][0]["evidence_group"] == "artifact-finalization"
