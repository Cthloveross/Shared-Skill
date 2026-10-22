"""Resolve and verify frozen experiment evidence after a byte-preserving move.

The archive index maps old roots; the existing audit supplies file digests.  This
module never imports historical controllers, rewrites evidence, or calls models.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterator


class ArchiveError(ValueError):
    pass


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ArchiveError(f"Not a normalized absolute path: {value}")
    return path


def _regular(path: Path, root: Path) -> bool:
    # Reject symlinks in any ancestor, including links that stay inside root.
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ArchiveError(f"Symlink or containment violation: {path}")
    return path.is_file()


class Archive:
    def __init__(self, index: Path, repository: Path | None = None):
        self.index = index.resolve()
        self.repository = (repository or self.index.parents[2]).resolve()
        self.data = json.loads(self.index.read_text())
        if self.data.get("schema") != "sharedskill-experiment-archive-v1":
            raise ArchiveError("Unsupported archive index")
        self.roots = sorted(
            self.data["roots"], key=lambda item: len(item["original"]), reverse=True
        )

    def resolve(self, original: str) -> Path:
        path = _absolute(original)
        for mapping in self.roots:
            old = _absolute(mapping["original"])
            if not path.is_relative_to(old):
                continue
            matches = []
            for mirror in mapping["mirrors"]:
                root = self.repository / mirror
                if not root.is_absolute() or ".." in Path(mirror).parts:
                    raise ArchiveError(f"Invalid archive mirror: {mirror}")
                if not root.is_relative_to(self.repository):
                    raise ArchiveError(f"Archive mirror escapes repository: {mirror}")
                candidate = root / path.relative_to(old)
                if _regular(candidate, root):
                    matches.append(candidate)
            if len(matches) != 1:
                raise ArchiveError(
                    f"Expected one archived regular file, found {len(matches)}: {original}"
                )
            return matches[0]
        raise ArchiveError(f"No archive root for {original}")

    def read_reference(self, reference: dict[str, str]) -> tuple[Path, str]:
        path = self.resolve(reference["path"])
        actual = digest(path)
        if actual != reference["sha256"]:
            raise ArchiveError(f"Digest mismatch: {reference['path']}")
        return path, actual

    def verify(self) -> dict[str, Any]:
        checked: dict[str, str] = {}
        byte_count = 0
        errors: list[str] = []

        def check(reference: dict[str, str]) -> Path | None:
            nonlocal byte_count
            original, expected = reference["path"], reference["sha256"]
            if original in checked:
                if checked[original] != expected:
                    errors.append(f"Conflicting expected digests: {original}")
                return None
            try:
                path, actual = self.read_reference(reference)
                checked[original] = actual
                byte_count += path.stat().st_size
                return path
            except (ArchiveError, OSError) as error:
                errors.append(str(error))
                return None

        audit = check(self.data["audit"])
        evidence_count = 0
        proof: dict[str, Any] = {}
        if audit:
            document = json.loads(audit.read_text())
            proof = document["proof"]
            evidence = proof["evidence"]
            evidence_count = len(evidence)
            for reference in evidence:
                check(reference)
        for record in self.data.get("terminal_records", []):
            path = check(record)
            if path:
                for reference in references(json.loads(path.read_text())):
                    check(reference)
        return {
            "run_id": self.data["run_id"],
            "verified": not errors,
            "audit_evidence_references": evidence_count,
            "unique_files_verified": len(checked),
            "bytes_verified": byte_count,
            "science_complete": proof.get("science_complete"),
            "requested_rounds": proof.get("requested_rounds"),
            "unique_actual_Day_executions": proof.get("unique_actual_Day_executions"),
            "Day_arm_views": proof.get("Day_arm_views"),
            "valid_S3_observations": proof.get("valid_S3_observations"),
            "retained": self.data.get("retained", []),
            "errors": errors,
        }


def references(value: Any) -> Iterator[dict[str, str]]:
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and re.fullmatch(
            r"[0-9a-f]{64}", str(value.get("sha256", ""))
        ):
            yield {"path": value["path"], "sha256": value["sha256"]}
        for child in value.values():
            yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "resolve"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--path", help="Original absolute path, for resolve")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.run_id):
        parser.error("run-id must be one directory name")
    repository = Path(__file__).resolve().parents[1]
    archive = Archive(repository / "artifacts" / args.run_id / "archive.json", repository)
    if args.command == "resolve":
        if not args.path:
            parser.error("resolve requires --path")
        print(archive.resolve(args.path))
        return 0
    result = archive.verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
