"""Resolve relocated experiment definitions without rewriting frozen JSON."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_EXPERIMENT_ROOTS = {
    Path("experiments/poisoned-evolution-benign-spreadsheetbench-4x25"): Path(
        "experiments/poisoned_evolution/benign"
    ),
    Path("experiments/poisoned-evolution-canary-spreadsheetbench-4x25"): Path(
        "experiments/poisoned_evolution/canary"
    ),
}


def resolve_path(
    value: str | Path,
    *,
    repo_root: Path = REPO_ROOT,
    follow_symlinks: bool = True,
) -> Path:
    """Keep repository-relative semantics and map only the two moved roots.

    Archived paths under artifacts/outputs are untouched. Interpreter paths
    can retain their venv symlink identity with ``follow_symlinks=False``.
    """

    root = Path(repo_root).expanduser().resolve()
    path = Path(value).expanduser()
    path = Path(os.path.abspath(path if path.is_absolute() else root / path))
    for old, new in _EXPERIMENT_ROOTS.items():
        try:
            relative = path.relative_to(root / old)
        except ValueError:
            continue
        path = root / new / relative
        break
    return path.resolve() if follow_symlinks else path
