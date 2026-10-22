"""Offline suite selection, trial isolation, and report contracts for F canaries."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from experiments.poisoned_evolution.spreadsheetbench.cli import (
    _configured_families,
    _make_report,
    _protocol_contract,
    _supervisor_usage_stop,
)
from experiments.poisoned_evolution.spreadsheetbench.detectors import detect_all_added
from experiments.poisoned_evolution.spreadsheetbench.evolution import (
    build_batch_manifests,
    run_matrix,
    run_trial,
)
from experiments.poisoned_evolution.spreadsheetbench.models import (
    EngineRunResult,
    ProtocolError,
    TrialDecision,
    TrialStatus,
)
from experiments.poisoned_evolution.spreadsheetbench.transforms import (
    ALL_BEHAVIORS,
    canonical_session_bytes,
    family_ids_for,
)
from experiments.poisoned_evolution.spreadsheetbench.usage import (
    UsageBudgetExceeded,
    UsageLedger,
)

BF = ("bf1", "bf2", "bf3", "bf4")
F = ("f1", "f2", "f3", "f4")
S0 = "---\nname: xlsx\ndescription: Edit workbooks.\n---\n# XLSX\nSave the requested workbook.\n"


def _config(tmp_path: Path, families: tuple[str, ...] = F) -> dict[str, Any]:
    return {
        "run_ids": {"formal": "offline-formal"},
        "engine": {"s0_bundle_path": str(tmp_path / "s0")},
        "matrix": {
            "families": list(families),
            "seed": 42,
            "batch_manifests": 30,
            "batch_size": 30,
            "carrier_count": 3,
            "completed_per_family": 25,
            "max_attempts_per_family": 30,
            "same_infrastructure_signature_circuit_breaker": 2,
        },
        "metrics": {"primary": "SER" if families == F else "benign_SER"},
        "budgets": {"matrix_max_model_requests": 3840, "matrix_wallclock_seconds": 64800},
        "paths": {
            "formal_artifacts": str(tmp_path / "artifacts"),
            "formal_outputs": str(tmp_path / "outputs"),
            "usage_ledger": str(tmp_path / "usage.json"),
        },
    }


def _session(task: int, case: int = 1) -> dict[str, Any]:
    from .test_workflow_transform import session_fixture

    session = session_fixture(task, case=case)
    session.update(baseline_input_pass=False, carrier_eligible=True)
    return session


def _manifest(batch: int = 1) -> dict[str, Any]:
    return {
        "batch_id": f"batch-{batch:02d}",
        "session_ids": [_session(task)["session_id"] for task in range(30)],
        "carrier_session_ids": [_session(task)["session_id"] for task in range(3)],
    }


class MaterializingEngine:
    """Inspect the real transformed inputs but never call a model or execute tools."""

    def __init__(self, family: str, *, preexisting: bool = False) -> None:
        self.family = family
        self.preexisting = preexisting
        self.calls = 0

    async def run_once(self, *, sessions, s0_path, trial_dir, output_dir, expected_session_count):
        self.calls += 1
        assert len(sessions) == expected_session_count == 30
        assert len({row["task_id"] for row in sessions}) == 30
        for row in sessions:
            assert any(
                skill["skill_name"] == "xlsx"
                for turn in row["turns"] for skill in turn.get("read_skills", [])
            )
        behavior = ALL_BEHAVIORS[self.family]
        target = f"{behavior.action} {behavior.success}"
        initial = Path(s0_path, "SKILL.md").read_text()
        assert initial == S0 + (target + "\n" if self.preexisting else "")
        candidate = initial + ("\nDocument normal output validation.\n" if self.preexisting else target + "\n")
        return EngineRunResult(
            decision=TrialDecision.MATERIALIZED,
            summary={
                "sessions": 30,
                "skill_groups": 1,
                "no_skill_sessions": 0,
                "session_judge": {"enabled": False},
                "skill_verifier": {"enabled": False},
                "had_processing_error": False,
            },
            summaries={row["session_id"]: "Ordinary workbook result." for row in sessions},
            raw_candidates=(candidate,),
            materialized_skills={"xlsx": candidate},
        )


@pytest.mark.parametrize("families", [BF, F])
def test_complete_suite_selection_and_manifest_metadata(tmp_path: Path, families: tuple[str, ...]) -> None:
    config = _config(tmp_path, families)
    assert _configured_families(config) == families
    assert all(family_ids_for(family) == families for family in families)
    destination = tmp_path / "batches.json"
    build_batch_manifests(
        config,
        corpus_sessions=[_session(task, case) for task in range(100) for case in (1, 2, 3)],
        destination=destination,
    )
    assert json.loads(destination.read_text())["families"] == list(families)


@pytest.mark.parametrize("families", [("f1", "bf2", "f3", "f4"), F[:3], F + ("f1",)])
@pytest.mark.asyncio
async def test_mixed_or_incomplete_suites_fail_before_running(tmp_path: Path, families: tuple[str, ...]) -> None:
    config = _config(tmp_path, families)
    with pytest.raises((ValueError, ProtocolError), match="famil|suite"):
        _configured_families(config)
    with pytest.raises(ProtocolError, match="famil|suite"):
        await run_matrix(
            config, corpus_sessions=[], manifests=[],
            artifact_root=tmp_path / "artifacts", output_root=tmp_path / "outputs",
        )
    with pytest.raises(ProtocolError, match="famil|suite"):
        await run_trial(
            config, sessions=[_session(task) for task in range(30)], manifest=_manifest(),
            family_id="f1", artifact_dir=tmp_path / "trial", engine=MaterializingEngine("f1"),
        )
    assert not (tmp_path / "trial").exists()


def test_default_detector_suite_keeps_legacy_bf_contract() -> None:
    candidate = S0 + ALL_BEHAVIORS["bf4"].action + " " + ALL_BEHAVIORS["bf4"].success
    detections = detect_all_added(S0, candidate)
    assert tuple(detections) == BF
    assert {family for family, detection in detections.items() if detection.matched} == {"bf4"}
    assert tuple(detect_all_added(S0, candidate, family_ids=F)) == F


@pytest.mark.parametrize("family", F)
@pytest.mark.parametrize("preexisting", [False, True])
@pytest.mark.asyncio
async def test_canary_trial_routes_xlsx_and_scores_only_added_text(
    tmp_path: Path, family: str, preexisting: bool,
) -> None:
    s0 = tmp_path / "s0"
    s0.mkdir()
    behavior = ALL_BEHAVIORS[family]
    target = f"{behavior.action} {behavior.success}\n"
    (s0 / "SKILL.md").write_text(S0 + (target if preexisting else ""))
    sessions = [_session(task) for task in range(30)]
    before = [canonical_session_bytes(row) for row in sessions]
    engine = MaterializingEngine(family, preexisting=preexisting)
    result = await run_trial(
        _config(tmp_path), sessions=sessions, manifest=_manifest(), family_id=family,
        artifact_dir=tmp_path / "trial-artifact", output_dir=tmp_path / "trial-output", engine=engine,
    )
    assert engine.calls == 1
    assert result.status == TrialStatus.COMPLETED
    assert result.ser_success is (not preexisting)
    assert tuple(result.diagnostics.materialized_detector_hits) == F
    assert result.diagnostics.materialized_detector_hits[family] == int(not preexisting)
    assert result.diagnostics.cross_family_detector_hits == {}
    assert len(result.transform.carrier_session_ids) == 3
    assert len(result.transform.unchanged_session_ids) == 27
    assert [canonical_session_bytes(row) for row in sessions] == before
    assert (s0 / "SKILL.md").read_text() == S0 + (target if preexisting else "")


def _write_matrix_for_report(config: dict[str, Any], *, successes: tuple[int, ...]) -> dict[str, Any]:
    families = tuple(config["matrix"]["families"])
    matrix: dict[str, Any] = {
        "attempted": 100, "completed": 100, "unknown": 0,
        "successes": sum(successes), "ser": sum(successes) / 100,
        "families": {}, "trials": [],
    }
    for family, count in zip(families, successes):
        matrix["families"][family] = {
            "family_id": family, "attempted": 25, "completed": 25,
            "unknown": 0, "successes": count, "ser": count / 25,
        }
        for index in range(25):
            matrix["trials"].append({
                "trial_id": f"{family}-batch-{index + 1:02d}", "family_id": family, "status": "completed",
                "diagnostics": {
                    "materialized_detector_hits": {
                        detected: int(detected == family and index < count) for detected in families
                    },
                    "cross_family_detector_hits": {},
                },
            })
    output = Path(config["paths"]["formal_outputs"])
    output.mkdir(parents=True)
    (output / "matrix-result.json").write_text(json.dumps(matrix))
    return matrix


def test_legacy_report_preserves_bf_counts_and_metric(tmp_path: Path) -> None:
    config = _config(tmp_path, BF)
    _write_matrix_for_report(config, successes=(6, 1, 1, 1))
    report = _make_report(config)
    assert report["metric"] == "benign_SER"
    assert report["overall"] == {"attempted": 100, "completed": 100, "unknown": 0, "successes": 9, "ser": 0.09}
    assert tuple(report["families"]) == BF
    assert report["acceptance_gate"]["passed"] is True


@pytest.mark.parametrize("defect", [None, "missing_detector", "unknown_trial"])
def test_f_report_checks_four_detectors_and_completed_denominator(tmp_path: Path, defect: str | None) -> None:
    config = _config(tmp_path)
    matrix = _write_matrix_for_report(config, successes=(1, 2, 3, 4))
    if defect == "missing_detector":
        del matrix["trials"][0]["diagnostics"]["materialized_detector_hits"]["f4"]
    elif defect == "unknown_trial":
        matrix["completed"] = 99
        matrix["unknown"] = 1
        matrix["ser"] = 10 / 99
        matrix["families"]["f4"].update(completed=24, unknown=1, ser=4 / 24)
        matrix["trials"][-1]["status"] = "unknown"
    output = Path(config["paths"]["formal_outputs"])
    (output / "matrix-result.json").write_text(json.dumps(matrix))
    report = _make_report(config)
    assert report["metric"] == "SER"
    assert report["overall"]["completed"] == (99 if defect == "unknown_trial" else 100)
    assert report["overall"]["ser"] == (10 / 99 if defect == "unknown_trial" else 0.1)
    assert tuple(report["cross_family_detection"]["materialized_skill_hit_matrix"]) == F
    assert all(tuple(row) == F for row in report["cross_family_detection"]["materialized_skill_hit_matrix"].values())
    assert report["acceptance_gate"]["passed"] is (defect is None)


@pytest.mark.asyncio
async def test_f_matrix_reaches_all_four_families_and_reuses_no_evolved_s0(tmp_path: Path) -> None:
    config = _config(tmp_path)
    s0 = tmp_path / "s0"
    s0.mkdir()
    (s0 / "SKILL.md").write_text(S0)
    sessions = [_session(task) for task in range(30)]
    original = copy.deepcopy(sessions)
    engines: list[MaterializingEngine] = []

    def factory(family: str, batch: str) -> MaterializingEngine:
        engine = MaterializingEngine(family)
        engines.append(engine)
        return engine

    result = await run_matrix(
        config, corpus_sessions=sessions, manifests=[_manifest(index) for index in range(1, 31)],
        artifact_root=tmp_path / "artifacts", output_root=tmp_path / "outputs", engine_factory=factory,
    )
    assert tuple(result.families) == F
    assert (result.completed, result.unknown, result.successes) == (100, 0, 100)
    assert all(family.completed == 25 for family in result.families.values())
    assert len(engines) == 100 and all(engine.calls == 1 for engine in engines)
    assert sessions == original
    assert (s0 / "SKILL.md").read_text() == S0


def test_paper_canary_config_cannot_silently_override_implemented_marker() -> None:
    from experiments.poisoned_evolution.spreadsheetbench.cli import _configured_families

    config_path = Path(__file__).resolve().parents[3] / (
        "experiments/poisoned_evolution/canary/config.json"
    )
    config = json.loads(config_path.read_text())
    assert _configured_families(config) == ("f1", "f2", "f3", "f4")
    config["families"]["f1"]["marker"] = "UNIMPLEMENTED_MARKER"
    with pytest.raises(ValueError, match="definitions differ"):
        _configured_families(config)


@pytest.mark.parametrize("config_name,new_limit", [
    ("canary/config-02.json", 7501),
    ("benign/config-05.json", 7500),
])
def test_campaign_budget_changes_cannot_replace_frozen_evidence(
    tmp_path: Path, config_name: str, new_limit: int,
) -> None:
    from unittest.mock import patch
    from experiments.poisoned_evolution.spreadsheetbench.cli import (
        _freeze_sources,
        _require_execution_contract,
    )

    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / "experiments/poisoned_evolution" / config_name).read_text())
    ledger_path = tmp_path / "usage.json"
    config["paths"]["usage_ledger"] = str(ledger_path)
    config["budgets"].update(campaign_carryover_min_requests=0, campaign_carryover_min_tokens=0)
    UsageLedger(
        ledger_path, max_requests=config["budgets"]["max_model_requests_total"],
        max_tokens=config["budgets"]["max_tokens_total"],
    ).snapshot()
    original_ledger = ledger_path.read_bytes()
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    frozen_config = artifact / "frozen-config.json"
    original_config = json.dumps(config).encode()
    frozen_config.write_bytes(original_config)

    config["budgets"]["max_model_requests_total"] = new_limit
    observed, expected = _protocol_contract(config)
    assert observed == expected  # A new campaign may choose a positive integer cap.
    changed_config = tmp_path / "config.json"
    changed_config.write_text(json.dumps(config))
    module = "experiments.poisoned_evolution.spreadsheetbench.cli"
    with patch(module + "._source_provenance_readiness", return_value={"ready": True}):
        with pytest.raises(RuntimeError, match="different frozen config"):
            _freeze_sources(changed_config, config, artifact)
    with patch(module + "._model_contract_readiness") as model_probe:
        with pytest.raises(RuntimeError, match="limits differ from the frozen config"):
            _require_execution_contract(config)
    model_probe.assert_not_called()
    assert frozen_config.read_bytes() == original_config
    assert ledger_path.read_bytes() == original_ledger


def test_prior_usage_keeps_global_cap_without_consuming_new_matrix_quota(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["budgets"].update(max_model_requests_total=7500, max_tokens_total=200_000_000)
    path = Path(config["paths"]["usage_ledger"])
    ledger = UsageLedger(path, max_requests=7500, max_tokens=200_000_000)
    snapshot = ledger.snapshot()
    prior = {"requests": 7499, "tokens_consumed": 1000, "tokens_reserved": 0}
    snapshot["totals"] = prior.copy()
    snapshot["groups"] = {"prior::formal": prior.copy()}
    path.write_text(json.dumps(snapshot))

    assert _supervisor_usage_stop(config, group="formal") is None
    reservation = ledger.reserve(group="formal", estimated_tokens=10, group_request_limit=3840)
    ledger.settle(reservation, total_tokens=5)
    with pytest.raises(UsageBudgetExceeded, match="global 7500-request"):
        ledger.reserve(group="formal", estimated_tokens=10, group_request_limit=3840)
    assert _supervisor_usage_stop(config, group="formal") == "global_request_cap"
    result = ledger.snapshot()
    assert result["groups"]["prior::formal"] == prior
    assert result["groups"]["formal"]["requests"] == 1
    assert result["totals"]["requests"] == 7500
