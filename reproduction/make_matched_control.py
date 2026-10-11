#!/usr/bin/env python3
"""Build a clean control that preserves every unaffected summarized session."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_batch(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"{path} must contain a JSON list of session objects")
    return payload


def build_matched_control(
    poisoned: list[dict[str, Any]],
    clean_pool: list[dict[str, Any]],
    marker: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    clean_by_id = {str(session.get("session_id")): session for session in clean_pool}
    if len(clean_by_id) != len(clean_pool):
        raise ValueError("clean pool contains missing or duplicate session IDs")

    attacker_ids = [str(session.get("session_id")) for session in poisoned if session.get("user_alias") == "attacker-b"]
    if not attacker_ids:
        raise ValueError("poisoned batch contains no attacker-b sessions")

    matched: list[dict[str, Any]] = []
    for session in poisoned:
        session_id = str(session.get("session_id"))
        if session_id not in attacker_ids:
            matched.append(session)
            continue
        replacement = clean_by_id.get(session_id)
        if replacement is None:
            raise ValueError(f"clean pool has no replacement for {session_id}")
        serialized = json.dumps(replacement, ensure_ascii=False, default=list)
        if replacement.get("user_alias") == "attacker-b" or marker in serialized:
            raise ValueError(f"replacement for {session_id} is not clean")
        matched.append(replacement)

    if len(matched) != len(poisoned):
        raise AssertionError("matched control changed the batch size")
    if any(session.get("user_alias") == "attacker-b" for session in matched):
        raise AssertionError("matched control still contains an attacker alias")
    if marker in json.dumps(matched, ensure_ascii=False, default=list):
        raise AssertionError("matched control still contains the target marker")
    return matched, attacker_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poisoned", required=True)
    parser.add_argument("--clean-pool", required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    poisoned_path = Path(args.poisoned).resolve()
    clean_pool_path = Path(args.clean_pool).resolve()
    output_path = Path(args.output).resolve()
    poisoned = load_batch(poisoned_path)
    clean_pool = load_batch(clean_pool_path)
    matched, replaced = build_matched_control(poisoned, clean_pool, args.marker)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(matched, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "sessions": len(matched),
                "replaced": replaced,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
