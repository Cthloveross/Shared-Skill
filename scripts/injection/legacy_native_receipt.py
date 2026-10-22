"""Explicit migration proofs for completed native runs predating checkpoints.

These receipts record a posthoc, read-only observation of old real artifacts.
They do not create or retroactively claim a historical native checkpoint.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import shutil

POLICY = "legacy-completed-native-migration-v1"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read(path: Path):
    return json.loads(path.read_text())


def evidence(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": digest(path.read_bytes())}


def tree(root: Path) -> str:
    from scripts.wildclawbench.protocol import tree_hash
    return tree_hash(root)


def tree_evidence(root: Path) -> dict:
    return {"path": str(root.resolve()), "sha256": tree(root),
            "file_count": sum(p.is_file() for p in root.rglob("*"))}


def _compact(session: dict) -> bytes:
    return json.dumps({"session_id": session["session_id"], "task_id": session.get("task_id", ""),
        "num_turns": session.get("num_turns", len(session.get("turns", []))),
        "aggregate": session.get("aggregate"), "_skills_referenced": sorted(session.get("_skills_referenced") or []),
        "_avg_prm": session.get("_avg_prm"), "_has_tool_errors": session.get("_has_tool_errors", False),
        "_trajectory": session.get("_trajectory", ""), "_summary": session.get("_summary", "")},
        ensure_ascii=False, indent=2).encode()


def inspect_legacy(attempt: Path, bank: Path, cycle_config: Path) -> dict:
    """Recompute evidence without executing tools, an Agent or a summarizer."""
    from scripts.wildclawbench import evolution

    from sharedskill.skill_bundle import read_skill_bundle

    attempt, bank, cycle_config = attempt.resolve(), bank.resolve(), cycle_config.resolve()
    workspace = attempt / "workspace"
    summary_path = attempt.parent / "proposal-summary.json"
    summary = read(summary_path)
    process = read(attempt / "agent-process.json")
    result = read(attempt / "native-result.json")
    quiet_path = attempt / "openclaw-home/agent-quiescence.json"
    quiet = read(quiet_path)
    if (summary.get("status") != "completed" or Path(summary["attempt_dir"]).resolve() != attempt
            or process.get("returncode") != 0 or result.get("agent_returncode") != 0
            or quiet.get("quiescent") is not True or quiet.get("surviving_writers") != []
            or quiet.get("agent_exit_code") != 0 or quiet.get("deadline_expired") is not False
            or summary["source_bank_sha256"] != tree(bank)):
        raise ValueError("Legacy proposal lacks genuine completed native execution and parent bank")
    transcripts = list((attempt / "openclaw-home/.openclaw/agents/main/sessions").glob("*.jsonl"))
    if len(transcripts) != 1:
        raise ValueError("Legacy native identity is ambiguous")
    transcript = transcripts[0]
    records = [json.loads(line) for line in transcript.read_bytes().splitlines() if line.strip()]
    sid = records[0].get("id")
    messages = [r["message"] for r in records if r.get("message", {}).get("role")]
    if (records[0].get("type") != "session" or not sid or transcript.stem != sid
            or Path(records[0].get("cwd", "")).resolve() != workspace
            or process.get("session_id") not in (None, sid)
            or not messages or messages[-1].get("role") != "assistant"
            or messages[-1].get("stopReason") != "stop"
            or not any(c.get("type") == "text" and c.get("text", "").strip() for c in messages[-1].get("content", []))):
        raise ValueError("Legacy proposal lacks its real terminal native session")
    bank_names = {p.name for p in bank.iterdir() if (p / "SKILL.md").is_file()}
    originals = [read(p) for p in sorted((attempt / "raw-sessions").glob("*.json"))]
    normalized = [evolution._normalize_session(s, bank_names, restrict_to_bank=False) for s in originals]
    ids = [s["session_id"] for s in normalized]
    archived = {read(p)["session_id"]: read(p) for p in (attempt / "native-summaries").glob("*.json")}
    if not ids or len(set(ids)) != len(ids) or set(archived) != set(ids) or result.get("sessions") != len(ids):
        raise ValueError("Legacy input archive is incomplete or ambiguous")
    for session in normalized:
        saved = archived[session["session_id"]]
        # Official summarization annotates only these fields; native data stay exact.
        if any(saved.get(key) != value for key, value in session.items()) or not saved.get("_summary"):
            raise ValueError("Legacy summary differs from its original normalized input")
    guide = attempt / "EVOLVE_AGENTS.md"
    fingerprint = evolution._hash({"bank_hash": summary["source_bank_sha256"], "sessions": normalized,
        "settings": summary["settings"], "native_prompt_sha256": digest(guide.read_bytes())})
    if fingerprint != summary.get("input_fingerprint"):
        raise ValueError("Legacy original input fingerprint changed")
    quarantine_path = attempt / "quarantined-changeset.json"
    quarantine = read(quarantine_path)
    exports = quarantine.get("bundles", [])
    changed = sorted(p.name for p in (workspace / "skills").iterdir() if (p / "SKILL.md").is_file()
        and read_skill_bundle(p) != read_skill_bundle(bank / p.name))
    if (not exports or sorted(b["name"] for b in exports) != changed
            or sorted(x["skill_name"] for x in result["evolutions"]) != changed):
        raise ValueError("Legacy exports do not cover the native changed skills")
    for bundle in exports:
        exported = Path(bundle["bundle_dir"]).resolve()
        if (exported != attempt / "candidate" / bundle["name"] or tree(exported) != bundle["bundle_sha256"]
                or read_skill_bundle(exported) != read_skill_bundle(workspace / "skills" / bundle["name"])):
            raise ValueError("Legacy original exported bundle bytes changed")
    cycle = read(cycle_config)
    closure = []
    for index, source in enumerate(cycle["sources"]):
        frozen = cycle_config.parent / "frozen-orchestrator" / (f"{index:02d}-" + Path(source["path"]).name)
        if digest(frozen.read_bytes()) != source["sha256"]:
            raise ValueError("Legacy declared frozen source changed")
        closure.append({"original_path": source["path"], "sha256": source["sha256"], "frozen_path": str(frozen)})
    compact_records = [[sid + ".json", digest(_compact(archived[sid])), 0o644] for sid in sorted(ids)]
    trees = [attempt / "raw-sessions", attempt / "native-summaries", workspace / "skills", attempt / "candidate"]
    files = [summary_path, attempt / "agent-process.json", attempt / "native-result.json", quiet_path,
             transcript, quarantine_path, guide, cycle_config]
    return {"policy": POLICY, "attempt": str(attempt), "parent_bank": str(bank), "parent_bank_sha256": tree(bank),
        "source_protocol_hash": summary["settings"]["protocol_hash"], "native_session_id": sid,
        "native_returncode": 0, "native_elapsed_seconds": process["elapsed_seconds"],
        "supervisor_quiescent": True, "surviving_writers": [], "deadline_expired": False,
        "original_input_fingerprint": fingerprint, "input_session_count": len(ids),
        "input_session_ids": ids, "changed_bundle_names": changed,
        "observed_trees": [tree_evidence(p) for p in trees], "original_evidence": [evidence(p) for p in files],
        "input_proof": {"method": "archived-normalized-inputs-and-original-fingerprint",
            "raw_archive_sha256": tree(attempt / "raw-sessions"),
            "native_summaries_sha256": tree(attempt / "native-summaries"),
            "compact_projection_sha256_at_migration": digest(json.dumps(compact_records, sort_keys=True).encode()),
            "historical_native_input_snapshot_absent": True, "no_files_reconstructed_or_written": True},
        "declared_frozen_sources": closure,
        "source_coverage": {"declared_source_count": len(closure), "complete_native_dependency_closure_frozen": False,
            "missing_historical_coverage": ["full native engine dependency tree", "full sharedskill dependency tree"],
            "archived_native_guide_verified_by_original_fingerprint": True},
        "normalization_implementation_at_migration": {
            "normalize_session_source_sha256": digest(inspect.getsource(evolution._normalize_session).encode()),
            "fingerprint_hash_source_sha256": digest(inspect.getsource(evolution._hash).encode())},
        "historical_native_flow_checkpoint_present": False, "old_rejection_preserved": True,
        "native_or_summary_calls": 0, "scores_or_skill_text_rewritten": False}


def create_legacy_receipt(attempt: Path, bank: Path, target: Path, *, cycle_config: Path) -> dict:
    facts = inspect_legacy(attempt, bank, cycle_config)
    target.mkdir(parents=True, exist_ok=False)
    shutil.copytree(attempt / "workspace/skills", target / "skills")
    if tree(target / "skills") != tree(attempt / "workspace/skills"):
        raise ValueError("Migration snapshot changed native skill bytes")
    record = {**facts, "skill_root": str((target / "skills").resolve()),
        "skill_root_sha256": tree(target / "skills"), "snapshot_kind": "posthoc-byte-exact-original-native-workspace-copy"}
    path = target / "receipt.json"
    path.write_text(json.dumps(record, sort_keys=True, ensure_ascii=False, indent=2) + "\n")
    return {"receipt": str(path.resolve()), "receipt_sha256": digest(path.read_bytes())}


def verify_legacy_receipt(attempt: Path, bank: Path, config: dict) -> dict:
    binding = config["native_legacy_origin"]
    path = Path(binding["receipt"])
    if digest(path.read_bytes()) != binding["receipt_sha256"]:
        raise ValueError("Legacy migration receipt changed")
    saved = read(path)
    if saved.get("policy") != POLICY or saved.get("attempt") != str(attempt.resolve()):
        raise ValueError("Legacy migration origin differs")
    cycle = next(item for item in saved["original_evidence"] if Path(item["path"]).name == "cycles-config.json")
    if tree(bank) != saved["parent_bank_sha256"]:
        raise ValueError("Legacy proposal belongs to another parent bank")
    facts = inspect_legacy(attempt, Path(saved["parent_bank"]), Path(cycle["path"]))
    if any(saved.get(key) != value for key, value in facts.items()):
        raise ValueError("Legacy original evidence changed after migration")
    skill_root = Path(saved["skill_root"])
    if (skill_root.resolve() != path.parent.resolve() / "skills" or tree(skill_root) != saved["skill_root_sha256"]
            or tree(skill_root) != tree(attempt / "workspace/skills")):
        raise ValueError("Legacy migration skill snapshot changed")
    return {"origin_kind": POLICY, "checkpoint": None, "checkpoint_sha256": None,
        "legacy_receipt": str(path), "legacy_receipt_sha256": binding["receipt_sha256"],
        "skill_root": str(skill_root), "native_returncode": 0,
        "native_session_id": saved["native_session_id"], "source_protocol_hash": saved["source_protocol_hash"],
        "workspace": str(attempt / "workspace"), "input_proof": saved["input_proof"], "report": None,
        "native_checkpoint_origin_explicit": False, "historical_native_flow_checkpoint_present": False,
        "source_coverage": saved["source_coverage"], "native_text_or_summary_reexecuted": False}
