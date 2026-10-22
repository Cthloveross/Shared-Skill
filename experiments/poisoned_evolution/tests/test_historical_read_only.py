"""Terminal experiments stay byte-identical when inspected or relaunched."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from experiments.poisoned_evolution.spreadsheetbench import cli


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _config(tmp_path: Path) -> tuple[Path, dict]:
    config = {
        "schema_version": "poisoned-evolution-spreadsheetbench-benign-v1",
        "run_ids": {kind: f"{kind}-run" for kind in ("corpus", "smoke", "formal")},
        "matrix": {"families": ["f1", "f2", "f3", "f4"], "completed_per_family": 25},
        "paths": {
            "artifacts_dir": str(tmp_path / "artifacts"),
            "outputs_dir": str(tmp_path / "outputs"),
            "usage_ledger": str(tmp_path / "ledger.json"),
        },
    }
    path = tmp_path / "config.json"
    _json(path, config)
    return path, config


def _terminal(config: dict, kind: str, aborted: bool = False) -> None:
    artifact, output, _ = cli._run_paths(config, kind)
    if aborted:
        _json(artifact / "controller.json", {"status": "aborted_by_user"})
    elif kind == "corpus":
        _json(output / "corpus/report.json", {"ready_for_trials": True})
    elif kind == "smoke":
        _json(artifact / "smoke-gate.json", {"status": "passed"})
    else:
        _json(output / "matrix-result.json", {"completed": 100, "unknown": 0})


def _snapshot(root: Path) -> dict:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "kind"),
    [("collect", "corpus"), ("smoke", "smoke"), ("run", "formal"),
     ("resume", "formal"), ("supervise", "formal")],
)
@pytest.mark.parametrize("aborted", [False, True])
async def test_terminal_commands_stop_before_locks_events_or_provider(
    tmp_path: Path, monkeypatch, command: str, kind: str, aborted: bool,
) -> None:
    path, config = _config(tmp_path)
    _terminal(config, kind, aborted)
    provider = Mock(side_effect=AssertionError("provider must not be reached"))
    contract = Mock(side_effect=AssertionError("must stop before execution checks"))
    monkeypatch.setattr(cli.httpx, "get", provider)
    monkeypatch.setattr(cli, "_require_execution_contract", contract)
    before = _snapshot(tmp_path)
    args = cli._parser().parse_args([command, "--config", str(path)])
    with pytest.raises(RuntimeError, match="historical artifacts are read-only"):
        await cli._async_main(args)
    assert _snapshot(tmp_path) == before
    assert not list(tmp_path.rglob("*.lock"))
    provider.assert_not_called()
    contract.assert_not_called()


def test_target_run_guards_preserve_unknown_resume(tmp_path: Path) -> None:
    _, config = _config(tmp_path)
    _terminal(config, "corpus")
    _terminal(config, "smoke")
    artifact, output, _ = cli._run_paths(config, "formal")
    _json(output / "matrix-result.json", {"completed": 99, "unknown": 1})
    _json(artifact / "controller.json", {"status": "completed_with_unknowns"})
    before = _snapshot(tmp_path)
    cli._require_writable_run(config, "formal")
    assert _snapshot(tmp_path) == before
    with pytest.raises(RuntimeError, match="corpus_complete"):
        cli._require_writable_run(config, "corpus")


def test_controller_claim_refuses_terminal_before_any_write(tmp_path: Path) -> None:
    path, config = _config(tmp_path)
    _terminal(config, "formal")
    before = _snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="read-only"):
        cli._claim_controller(path, config, kind="formal", command="resume")
    assert _snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_completed_supervisor_does_not_launch_or_write(tmp_path: Path) -> None:
    path, config = _config(tmp_path)
    artifact, _, _ = cli._run_paths(config, "formal")
    _json(artifact / "supervisor.json", {"status": "complete"})
    before = _snapshot(tmp_path)
    child = Mock(side_effect=AssertionError("no child may launch"))
    with pytest.raises(RuntimeError, match="read-only"):
        await cli._supervise(path, config, child_runner=child)
    child.assert_not_called()
    assert _snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_report", [False, True])
async def test_terminal_report_prints_frozen_bytes_or_refuses(
    tmp_path: Path, monkeypatch, existing_report: bool,
) -> None:
    path, config = _config(tmp_path)
    _terminal(config, "formal")
    _, output, _ = cli._run_paths(config, "formal")
    frozen = {"overall": {"completed": 100, "successes": 4, "ser": 0.04}}
    if existing_report:
        _json(output / "report.json", frozen)
    printed = Mock()
    monkeypatch.setattr(cli, "_print", printed)
    monkeypatch.setattr(cli, "_make_report", Mock(side_effect=AssertionError("must not regenerate")))
    before = _snapshot(tmp_path)
    args = cli._parser().parse_args(["report", "--config", str(path)])
    if existing_report:
        assert await cli._async_main(args) == 0
        printed.assert_called_once_with(frozen)
    else:
        with pytest.raises(RuntimeError, match="refusing to write"):
            await cli._async_main(args)
    assert _snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_exists", [False, True])
async def test_status_reads_ledger_without_snapshot_lock_or_write(
    tmp_path: Path, monkeypatch, ledger_exists: bool,
) -> None:
    path, config = _config(tmp_path)
    for kind in ("corpus", "smoke", "formal"):
        _terminal(config, kind)
    if ledger_exists:
        _json(Path(config["paths"]["usage_ledger"]), {
            "schema_version": 1,
            "limits": {"requests": 5000, "tokens": 200_000_000},
            "totals": {"requests": 100, "tokens_consumed": 2000, "tokens_reserved": 0},
            "groups": {}, "updated_at": "frozen-time",
        })
    forbidden = Mock(side_effect=AssertionError("status must not open writer/provider"))
    monkeypatch.setattr(cli, "ledger_from_config", forbidden)
    monkeypatch.setattr(cli.httpx, "get", forbidden)
    printed = Mock()
    monkeypatch.setattr(cli, "_print", printed)
    before = _snapshot(tmp_path)
    args = cli._parser().parse_args(["status", "--config", str(path)])
    assert await cli._async_main(args) == 0
    assert _snapshot(tmp_path) == before
    assert not list(tmp_path.rglob("*.lock"))
    forbidden.assert_not_called()
    if ledger_exists:
        assert printed.call_args.args[0]["usage"]["model_calls"] == 100


def test_abort_does_not_rewrite_any_completed_stage(tmp_path: Path, monkeypatch) -> None:
    _, config = _config(tmp_path)
    for kind in ("corpus", "smoke", "formal"):
        _terminal(config, kind)
    artifact, _, _ = cli._run_paths(config, "formal")
    _json(artifact / "supervisor.json", {"status": "complete"})
    signal = Mock(side_effect=AssertionError("no terminal process should be signalled"))
    monkeypatch.setattr(cli, "_signal_controller", signal)
    before = _snapshot(tmp_path)
    result = cli._abort(config)
    assert all(stage["status"] == "already_terminal" for stage in result["runs"].values())
    signal.assert_not_called()
    assert _snapshot(tmp_path) == before
