#!/usr/bin/env python3
"""Verify the causal invariants of one poison/control result pair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


class VerificationError(RuntimeError):
    pass


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_pair(poison_dir: Path, clean_dir: Path) -> dict[str, Any]:
    poison_run = load_json(poison_dir / "run.json")
    clean_run = load_json(clean_dir / "run.json")
    poison_sessions = load_json(poison_dir / "sessions.json")
    clean_sessions = load_json(clean_dir / "sessions.json")
    marker = str(poison_run["marker"])

    if poison_run.get("status") != "completed" or clean_run.get("status") != "completed":
        raise VerificationError("both runs must have status=completed")
    # Preserve read-only verification of historical, pre-rename artifacts.
    poison_commit = poison_run.get("sharedskill_commit", poison_run.get("skillclaw_commit"))
    clean_commit = clean_run.get("sharedskill_commit", clean_run.get("skillclaw_commit"))
    if poison_commit != clean_commit:
        raise VerificationError("paired metadata differs for sharedskill_commit")
    for key in ("model", "family", "n_used", "seed"):
        if poison_run.get(key) != clean_run.get(key):
            raise VerificationError(f"paired metadata differs for {key}")
    if (poison_dir / "initial-SKILL.md").read_bytes() != (clean_dir / "initial-SKILL.md").read_bytes():
        raise VerificationError("initial skills differ")
    if poison_run.get("embedded") is not True:
        raise VerificationError("poison run is not a detector positive")
    if clean_run.get("embedded") is not False:
        raise VerificationError("clean run is not a detector negative")
    if (clean_run.get("result") or {}).get("action") != "skip":
        raise VerificationError("clean evolver did not return skip")
    if not (poison_dir / "evolved-SKILL.md").is_file():
        raise VerificationError("poison run has no evolved skill")
    if marker not in (poison_dir / "evolved-SKILL.md").read_text(encoding="utf-8"):
        raise VerificationError("evolved skill does not contain the marker")
    if (clean_dir / "evolved-SKILL.md").exists():
        raise VerificationError("clean run unexpectedly has an evolved skill")

    poison_ids = [str(session.get("session_id")) for session in poison_sessions]
    clean_ids = [str(session.get("session_id")) for session in clean_sessions]
    if poison_ids != clean_ids:
        raise VerificationError("session IDs or ordering differ")
    clean_serialized = json.dumps(clean_sessions, ensure_ascii=False)
    if marker in clean_serialized:
        raise VerificationError("clean sessions contain the target marker")

    attacker_ids = {
        str(session.get("session_id")) for session in poison_sessions if session.get("user_alias") == "attacker-b"
    }
    evidence_ids = {
        str(session.get("session_id"))
        for session in poison_sessions
        if marker in f"{session.get('_trajectory', '')}\n{session.get('_summary', '')}"
    }
    prompt_ids = {
        str(session.get("session_id"))
        for session in poison_sessions
        if any(marker in str(turn.get("prompt_text", "")) for turn in session.get("turns", []))
    }
    if not attacker_ids or attacker_ids != evidence_ids:
        raise VerificationError("attacker aliases and marker-bearing evidence do not match")
    if prompt_ids:
        raise VerificationError(f"target marker leaked into user prompts: {sorted(prompt_ids)}")

    unchanged_ids: list[str] = []
    for poisoned, clean in zip(poison_sessions, clean_sessions, strict=True):
        session_id = str(poisoned.get("session_id"))
        if session_id in attacker_ids:
            continue
        if poisoned.get("_trajectory") != clean.get("_trajectory"):
            raise VerificationError(f"unaffected trajectory differs for {session_id}")
        if poisoned.get("_summary") != clean.get("_summary"):
            raise VerificationError(f"unaffected summary differs for {session_id}")
        unchanged_ids.append(session_id)

    return {
        "verified": True,
        "marker": marker,
        "attacker_ids": sorted(attacker_ids),
        "unaffected_sessions_identical": len(unchanged_ids),
        "poison_action": (poison_run.get("result") or {}).get("action"),
        "clean_action": (clean_run.get("result") or {}).get("action"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poison", type=Path, required=True)
    parser.add_argument("--clean", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = verify_pair(args.poison.resolve(), args.clean.resolve())
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
