"""Deterministic provenance helpers for the SpreadsheetBench reproduction."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path


def _tree_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError(f"tree digest does not accept symlinks: {path}")
        if path.is_file():
            yield path


def sha256_relative_files(root: str | Path, paths: Iterable[Path]) -> str:
    """Hash selected files using their paths relative to ``root``."""

    directory = Path(root)
    if not directory.is_dir():
        raise ValueError(f"tree digest requires a directory: {directory}")
    digest = hashlib.sha256()
    ordered = sorted(paths, key=lambda item: item.relative_to(directory).as_posix())
    for path in ordered:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"tree digest requires regular files: {path}")
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def sha256_tree(root: str | Path) -> str:
    """Hash a directory as length-prefixed relative paths and file bytes."""

    directory = Path(root)
    return sha256_relative_files(directory, _tree_files(directory))
