"""Relocation must preserve frozen configuration and isolated engine paths."""

from pathlib import Path

import pytest

from experiments.poisoned_evolution.spreadsheetbench.cli import DEFAULT_CONFIG, _load_config
from experiments.poisoned_evolution.spreadsheetbench.layout import REPO_ROOT, resolve_path


@pytest.mark.parametrize("variant", ["benign", "canary"])
def test_legacy_config_roots_resolve_without_editing_files(variant):
    old = Path(f"experiments/poisoned-evolution-{variant}-spreadsheetbench-4x25/config.json")
    expected = REPO_ROOT / f"experiments/poisoned_evolution/{variant}/config.json"
    original = expected.read_bytes()
    assert resolve_path(old) == expected
    assert resolve_path(REPO_ROOT / old) == expected
    assert _load_config(old)[0] == expected
    assert expected.read_bytes() == original


def test_archived_historical_paths_are_not_redirected():
    path = REPO_ROOT / (
        "artifacts/example/source/experiments/"
        "poisoned-evolution-canary-spreadsheetbench-4x25/config.json"
    )
    assert resolve_path(path) == path


def test_native_venv_interpreter_preserves_environment_identity(tmp_path):
    python = tmp_path / "bin/python"
    python.parent.mkdir()
    python.symlink_to("/usr/bin/python3")
    assert resolve_path(python, follow_symlinks=False) == python


def test_default_status_points_at_current_canary():
    path, config = _load_config(DEFAULT_CONFIG)
    assert path == REPO_ROOT / "experiments/poisoned_evolution/canary/config-06.json"
    assert config["run_ids"]["formal"] == "pe-canary-sb-qwen35-4x25-20261022-06"
