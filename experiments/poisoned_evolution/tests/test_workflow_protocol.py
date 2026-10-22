"""The corrected protocol keeps measured inputs and inference controls explicit."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from experiments.poisoned_evolution.spreadsheetbench.cli import _protocol_contract, _supervisor_next_action
from experiments.poisoned_evolution.spreadsheetbench.engine import E9cSubprocessEngine, RecordingBoundedLLM
from experiments.poisoned_evolution.spreadsheetbench.evolution import build_batch_manifests
from experiments.poisoned_evolution.spreadsheetbench.models import ProtocolError
from .test_pe_spreadsheetbench_evolution import _config, _session


def test_explicit_top_p_reaches_provider_and_native_stage_temperatures_are_preserved(tmp_path):
    wrapper = RecordingBoundedLLM(
        SimpleNamespace(model="local", temperature=0.4), log_path=tmp_path / "calls.jsonl",
        max_tokens=16384, chat_template_kwargs={"enable_thinking": False}, top_p=0.95,
    )
    messages = [{"role": "user", "content": "fixed native request"}]
    for temperature in (0.2, 0.4, 0.3):
        request = wrapper._provider_request(messages, {"temperature": temperature, "max_tokens": 100000})
        assert request["messages"] is messages
        assert request["temperature"] == temperature
        assert request["top_p"] == 0.95
        assert request["max_completion_tokens"] == 16384
        assert request["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    with pytest.raises(ProtocolError, match="top_p conflicts"):
        wrapper._provider_request(messages, {"top_p": 0.1})


def test_full_smoke_is_an_extra_batch_and_keeps_all_30_formal_manifests(tmp_path):
    sessions = [_session(task, case=case) for task in range(100) for case in (1, 2, 3)]
    config = _config(tmp_path)
    formal = build_batch_manifests(config, corpus_sessions=sessions, destination=tmp_path / "formal.json")
    combined = build_batch_manifests(
        config, corpus_sessions=sessions, destination=tmp_path / "combined.json", include_smoke=True,
    )
    assert combined[:30] == formal
    smoke = combined[-1]
    assert len(combined) == 31 and smoke["role"] == "smoke"
    assert len(set(smoke["task_ids"])) == 30
    assert len(smoke["carrier_session_ids"]) == 3
    assert smoke["session_ids"] not in [row["session_ids"] for row in formal]


def test_frozen_v2_config_has_effective_inference_and_budget_contract():
    config_path = Path(__file__).resolve().parents[1] / "canary/config-03.json"
    config = json.loads(config_path.read_text())
    observed, expected = _protocol_contract(config)
    assert observed == expected
    engine = E9cSubprocessEngine.from_config(config)
    assert engine.llm_top_p == 0.95 and engine.llm_temperature == 0.4
    assert engine.usage_max_requests - config["budgets"]["campaign_carryover_min_requests"] == 4000


def test_v3_requires_recalculation_projection_without_resetting_campaign_budget():
    config_path = Path(__file__).resolve().parents[1] / "canary/config-04.json"
    config = json.loads(config_path.read_text())
    observed, expected = _protocol_contract(config)
    assert observed == expected
    assert config["protocol_version"] == "workflow-canary-v3"
    assert observed["corpus.session_adapter"] == "execution_facts_v3"
    assert config["budgets"]["max_model_requests_total"] == 10585
    assert config["budgets"]["campaign_carryover_min_requests"] == 7225
    assert config["budgets"]["campaign_carryover_min_tokens"] == 27529246
    config["corpus"]["session_adapter"] = "execution_facts_v2"
    observed, expected = _protocol_contract(config)
    assert observed["corpus.session_adapter"] != expected["corpus.session_adapter"]


def test_qwen35_comparison_preserves_science_and_uses_its_own_effective_budget():
    from experiments.poisoned_evolution.spreadsheetbench.usage import ledger_from_config

    root = Path(__file__).resolve().parents[3]
    directory = Path(__file__).resolve().parents[1] / "canary"
    prior = json.loads((directory / "config-04.json").read_text())
    config = json.loads((directory / "config-05.json").read_text())
    for key in ("sources", "corpus", "engine", "families", "metrics", "protocol_version", "smoke"):
        assert config[key] == prior[key]
    for key, value in prior["matrix"].items():
        if key != "run_id":
            assert config["matrix"][key] == value
    for key, value in prior["model"].items():
        if key not in {"id", "revision", "base_url"}:
            assert config["model"][key] == value
    assert config["run_ids"]["corpus"] == prior["run_ids"]["corpus"]
    assert config["paths"]["corpus_outputs"] == prior["paths"]["corpus_outputs"]
    assert config["paths"]["usage_ledger"] != prior["paths"]["usage_ledger"]
    assert config["budgets"]["campaign_carryover_min_requests"] == 0
    assert config["budgets"]["campaign_carryover_min_tokens"] == 0
    observed, expected = _protocol_contract(config)
    assert observed == expected
    engine = E9cSubprocessEngine.from_config(config)
    ledger = ledger_from_config(config, repository_root=root)
    assert engine.llm_model == "Qwen3.5-35B-A3B"
    assert engine.llm_base_url == "http://127.0.0.1:18182/v1"
    assert engine.usage_max_requests == ledger.max_requests == 4000
    assert engine.usage_max_tokens == ledger.max_tokens == 50_000_000


@pytest.mark.parametrize("field", ["max_model_requests_total", "max_tokens_total"])
@pytest.mark.parametrize("value", [None, 0, -1, True, 4000.0, "4000", "invalid", [], {}])
def test_campaign_caps_reject_nonpositive_or_noninteger_values_before_provider(field, value):
    from unittest.mock import patch
    from experiments.poisoned_evolution.spreadsheetbench.cli import _require_execution_contract

    config = json.loads((Path(__file__).resolve().parents[1] / "canary/config-05.json").read_text())
    config["budgets"][field] = value
    observed, expected = _protocol_contract(config)
    assert observed != expected
    module = "experiments.poisoned_evolution.spreadsheetbench.cli"
    with patch(module + "._model_contract_readiness") as probe:
        with pytest.raises((RuntimeError, ValueError, TypeError)):
            _require_execution_contract(config)
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_comparison_reuses_completed_corpus_instead_of_collecting(tmp_path):
    from unittest.mock import patch
    from experiments.poisoned_evolution.spreadsheetbench.cli import (
        _acquisition_smoke,
        _load_corpus_sessions,
        _validated_acquisition_smoke,
    )

    root = Path(__file__).resolve().parents[3]
    config = json.loads((Path(__file__).resolve().parents[1] / "canary/config-05.json").read_text())
    sessions = _load_corpus_sessions(config)
    manifests = build_batch_manifests(
        config, corpus_sessions=sessions, destination=tmp_path / "batches.json", include_smoke=True,
    )
    prior = json.loads((root / "artifacts/pe-canary-sb-qwen-4x25-20261021-04/batch-manifests.json").read_text())
    assert list(manifests) == prior["batches"]

    # Runtime stage decisions must not depend on the live comparison's files.
    for kind, run_id in config["run_ids"].items():
        config["paths"][f"{kind}_artifacts"] = str(tmp_path / "artifacts" / run_id)
        config["paths"][f"{kind}_outputs"] = str(tmp_path / "outputs" / run_id)
    corpus_report = Path(config["paths"]["corpus_outputs"]) / "corpus/report.json"
    corpus_report.parent.mkdir(parents=True)
    corpus_report.write_text(json.dumps({"ready_for_trials": True}))
    module = "experiments.poisoned_evolution.spreadsheetbench.cli"
    with (
        patch(module + "._supervisor_usage_stop", return_value=None),
        patch(module + "._load_corpus_sessions", return_value=sessions),
        patch(module + "._freeze_sources"),
        patch(module + ".collect", side_effect=AssertionError("must reuse collection")) as collect,
        patch("experiments.poisoned_evolution.spreadsheetbench.corpus.score_workbook",
              side_effect=AssertionError("must reuse scores")) as score,
    ):
        action = _supervisor_next_action(config)
        assert action == {"terminal": False, "stage": "acquisition", "command": "smoke", "phase": "acquisition"}
        report = await _acquisition_smoke(tmp_path / "config.json", config)
        assert report["evidence_mode"] == "reuse_completed_cases"
        assert report["reexecuted"] is False and report["cases_completed"] == 9
        assert _validated_acquisition_smoke(config)["status"] == "9/9 determinate"
        assert _supervisor_next_action(config) == {
            "terminal": False, "stage": "evolution_smoke", "command": "smoke", "phase": "evolution",
        }
    collect.assert_not_called()
    score.assert_not_called()


@pytest.mark.parametrize("protocol", ["workflow-canary-v2", "workflow-canary-v3"])
def test_supervisor_derives_before_any_new_acquisition_calls(tmp_path, protocol):
    config = {
        "protocol_version": protocol,
        "run_ids": {"corpus": "corpus", "smoke": "smoke", "formal": "formal"},
        "paths": {"artifacts_dir": str(tmp_path / "a"), "outputs_dir": str(tmp_path / "o")},
    }
    from unittest.mock import patch
    module = "experiments.poisoned_evolution.spreadsheetbench.cli"
    with patch(module + "._corpus_report", return_value=None), patch(module + "._supervisor_usage_stop", return_value=None):
        action = _supervisor_next_action(config)
    assert action["command"] == "collect" and action["stage"] == "collection_reuse"
