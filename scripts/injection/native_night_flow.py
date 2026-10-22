"""Versioned native Night checkpoints and mechanical validation feedback.

This module does not publish a bank. Its successful output must still pass
source resolution, the selected deployment gates and the real A/B controller.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid


POLICY = "native-night-checkpoint-and-compact-loader-feedback-v2"
PER_SKILL_SCOPE = "per-skill-v1"


def per_skill(config: dict) -> bool:
    scope = config.get("alignment", {}).get("native_candidate_scope")
    if scope not in (None, "atomic", "atomic-v1", PER_SKILL_SCOPE):
        raise ValueError("Unknown native candidate validation scope")
    return scope == PER_SKILL_SCOPE


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(raw)


def tree_hash(root: Path, *, preserve_symlinks: bool = False) -> str:
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            if not preserve_symlinks:
                raise ValueError("Symlink in native Night checkpoint")
            records.append([path.relative_to(root).as_posix(), {"symlink": str(path.readlink())}, path.lstat().st_mode & 0o777])
            continue
        if path.is_file():
            records.append([path.relative_to(root).as_posix(), sha(path.read_bytes()), path.stat().st_mode & 0o777])
    return sha(json.dumps(records, sort_keys=True).encode())


def runtime_options(openclaw: dict, config: dict) -> dict:
    """Opt in to the installed official detector; retain its default thresholds."""
    if not config.get("native_night_flow"):
        return openclaw
    if config["native_night_flow"] != POLICY:
        raise ValueError("Unknown native Night runtime policy")
    result = copy.deepcopy(openclaw)
    result.setdefault("tools", {})["loopDetection"] = {"enabled": True}
    defaults = result.setdefault("agents", {}).setdefault("defaults", {})
    if defaults.get("compaction", {}).get("mode") != "safeguard":
        raise ValueError("Native Night continuation requires declared safeguard compaction")
    return result


def session_file(home: Path, session_id: str) -> Path:
    return home / ".openclaw/agents/main/sessions" / (session_id + ".jsonl")


def assert_session(path: Path, session_id: str, prefix: bytes = b"") -> bytes:
    raw = path.read_bytes()
    if not raw.startswith(prefix):
        raise ValueError("Native continuation changed its existing transcript prefix")
    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not records or records[0].get("type") != "session" or records[0].get("id") != session_id:
        raise ValueError("Native transcript identity differs")
    return raw


def snapshot(workspace: Path, home: Path, session_id: str, target: Path, config: dict, result) -> dict:
    transcript = session_file(home, session_id)
    raw = assert_session(transcript, session_id)
    quiet_path = home / "agent-quiescence.json"
    quiet = json.loads(quiet_path.read_text())
    if quiet.get("quiescent") is not True or quiet.get("surviving_writers"):
        raise ValueError("Native Night writers are not confirmed stopped")
    target.mkdir(parents=True, exist_ok=False)
    shutil.copytree(workspace / "skills", target / "skills", symlinks=per_skill(config))
    (target / "transcript.jsonl").write_bytes(raw)
    (target / "agent-quiescence.json").write_bytes(quiet_path.read_bytes())
    record = {"policy": POLICY, "session_id": session_id,
        "workspace": str(workspace), "openclaw_home": str(home),
        "transcript_sha256": sha(raw), "transcript_bytes": len(raw),
        "skills_sha256": tree_hash(workspace / "skills", preserve_symlinks=per_skill(config)),
        "session_inputs_sha256": tree_hash(workspace / "sessions"),
        "parent_bank_sha256": tree_hash(Path(config["_proposal_source_bank"])),
        "protocol_hash": config["protocol_hash"], "returncode": result.returncode,
        "source_session_continued": True, "agent_quiescent": True}
    if per_skill(config):
        shutil.copytree(workspace / "sessions", target / "session-inputs")
        if tree_hash(target / "session-inputs") != record["session_inputs_sha256"]:
            raise ValueError("Native input snapshot bytes changed")
        record["native_candidate_scope"] = PER_SKILL_SCOPE
    write(target / "checkpoint.json", record)
    return record


def source_validation(config: dict, bundles: list, bank: dict, sessions: list):
    from scripts.injection import source_evidence_v2 as source

    policy = config.get("alignment", {}).get("source_evidence_revision")
    if policy not in {source.POLICY, getattr(source, "POLICY_V3", source.POLICY)}:
        raise ValueError("Native Night source policy is not a frozen supported policy")
    if hasattr(source, "resolve_history_sources_for_config"):
        return source.resolve_history_sources_for_config(config, bundles, bank, sessions)
    # Preserve the already frozen v2 behavior when reading an older source tree.
    if policy != source.POLICY:
        raise ValueError("Declared native source policy is unavailable")
    return source.resolve_history_sources(bundles, bank, sessions)


def mechanical_validation(workspace: Path, config: dict) -> dict:
    """Actual deployment/load checks plus exact source evidence for every edit."""
    if per_skill(config):
        return mechanical_validation_per_skill(workspace, config)
    from scripts.wildclawbench.environment import preflight_candidate_bank
    from scripts.wildclawbench.protocol import tree_hash as bank_hash
    from sharedskill.skill_bundle import read_skill_bundle
    SOURCE_POLICY = config.get("alignment", {}).get("source_evidence_revision")

    current = workspace / "skills"
    parent = Path(config["_proposal_source_bank"])
    before = {path.name: read_skill_bundle(path) for path in parent.iterdir() if (path / "SKILL.md").is_file()}
    after = {path.name: read_skill_bundle(path) for path in current.iterdir() if (path / "SKILL.md").is_file()}
    changed = [{"name": name, "bundle_dir": str(current / name)} for name in sorted(after)
               if after[name] != before.get(name)]
    report = {"passed": False, "policy": POLICY, "source_policy": SOURCE_POLICY,
              "candidate_bank_sha256": bank_hash(current), "parent_bank_sha256": bank_hash(parent),
              "sessions_directory": str(workspace / "sessions"), "protocol_hash": config["protocol_hash"]}
    report["native_loader"] = preflight_candidate_bank(current,
        {"candidate_id": "night-selfcheck-" + report["candidate_bank_sha256"][:24]}, config)
    try:
        if set(before) - set(after):
            raise ValueError("The native workspace deleted an existing skill; restore its original bytes")
        sessions = config.get("_night_day_sessions")
        if not isinstance(sessions, list) or not sessions:
            raise ValueError("Mechanical source validation lacks its actual normalized Day evidence")
        selected, evidence = source_validation(config, changed, before, sessions)
        report["source_evidence"] = {"passed": True, "changed_bundle_names": [b["name"] for b in changed],
            "source_session_ids": sorted(s["source_session_id"] for s in selected),
            "source_task_ids": sorted({s["task_id"] for s in selected}), "evidence": evidence}
    except (OSError, ValueError, KeyError, TypeError) as error:
        report["source_evidence"] = {"passed": False, "error": f"{type(error).__name__}: {error}"}
    report["passed"] = (report["native_loader"].get("passed") is True
                        and report["source_evidence"].get("passed") is True)
    return report



def mechanical_validation_per_skill(workspace: Path, config: dict) -> dict:
    """Check one unchanged parent plus one exact bundle, independently for each edit."""
    from scripts.wildclawbench.environment import preflight_candidate_bank
    from scripts.wildclawbench.protocol import tree_hash as bank_hash
    from sharedskill.skill_bundle import read_skill_bundle
    SOURCE_POLICY = config.get("alignment", {}).get("source_evidence_revision")

    current, parent = workspace / "skills", Path(config["_proposal_source_bank"])
    before = {p.name: read_skill_bundle(p) for p in parent.iterdir() if (p / "SKILL.md").is_file()}
    sessions = config.get("_night_day_sessions")
    if not SOURCE_POLICY or not isinstance(sessions, list) or not sessions:
        raise ValueError("Per-skill validation lacks the frozen source policy or actual Day inputs")
    report = {"passed": False, "policy": POLICY, "native_candidate_scope": PER_SKILL_SCOPE,
        "source_policy": SOURCE_POLICY,
        "candidate_bank_sha256": tree_hash(current, preserve_symlinks=True),
        "candidate_bank_hash_kind": "native-checkpoint-path-bytes-mode-and-links-v1",
        "parent_bank_sha256": bank_hash(parent), "protocol_hash": config["protocol_hash"],
        "sessions_directory": str(workspace / "sessions"), "bundles": []}
    names = set(before) | {p.name for p in current.iterdir() if p.is_dir()}
    for name in sorted(names):
        bundle = current / name
        item = {"name": name, "bundle_dir": str(bundle), "eligible": False}
        try:
            if bundle.is_symlink() or any(path.is_symlink() for path in bundle.rglob("*")):
                raise ValueError("Symlink in candidate bundle")
            if not (bundle / "SKILL.md").is_file():
                raise ValueError("Existing skill deleted" if name in before else "Missing SKILL.md")
            after = read_skill_bundle(bundle)
            if after == before.get(name):
                continue
            substantive = lambda files: {key: value for key, value in files.items() if not key.startswith("history/")}
            if name in before and substantive(after) == substantive(before[name]):
                item.update(status="history_only_ignored", bundle_sha256=bank_hash(bundle))
                report["bundles"].append(item)
                continue
            item.update(action="improve" if name in before else "create", bundle_sha256=bank_hash(bundle))
            # Other changed siblings never enter this projection. It is destroyed
            # after the real loader checks; original Agent files are never repaired.
            with tempfile.TemporaryDirectory(prefix="native-one-bundle-") as directory:
                projected = Path(directory) / "bank"
                shutil.copytree(parent, projected)
                target = projected / name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(bundle, target)
                item["projected_bank_sha256"] = bank_hash(projected)
                item["native_loader"] = preflight_candidate_bank(projected,
                    {"candidate_id": "night-skill-" + sha((name + item["projected_bank_sha256"]).encode())[:24]}, config)
            try:
                selected, evidence = source_validation(config, [{"name": name, "bundle_dir": str(bundle)}], before, sessions)
                item["source_evidence"] = {"passed": True,
                    "source_session_ids": sorted(s["source_session_id"] for s in selected),
                    "source_task_ids": sorted({s["task_id"] for s in selected}), "evidence": evidence}
            except (OSError, ValueError, KeyError, TypeError) as error:
                item["source_evidence"] = {"passed": False, "error": f"{type(error).__name__}: {error}"}
            item["eligible"] = item["native_loader"].get("passed") is True and item["source_evidence"].get("passed") is True
            item["status"] = "mechanically_valid" if item["eligible"] else "mechanically_invalid"
        except (OSError, ValueError, KeyError, TypeError) as error:
            item.update(status="mechanically_invalid", error=f"{type(error).__name__}: {error}")
        report["bundles"].append(item)
    report["eligible_bundle_names"] = [b["name"] for b in report["bundles"] if b["eligible"]]
    report["invalid_bundle_names"] = [b["name"] for b in report["bundles"] if b["status"] == "mechanically_invalid"]
    report["passed"] = not report["invalid_bundle_names"]
    return report


def checkpoint_input_proof(attempt: Path, point: Path, expected: str) -> dict:
    """Verify retained bytes, or exact official compact bytes after server cleanup."""
    directory = attempt / "workspace/sessions"
    if (point / "session-inputs").exists():
        actual, kind = tree_hash(point / "session-inputs"), "checkpoint_session_inputs"
        if directory.exists() and any(directory.iterdir()) and tree_hash(directory) != expected:
            raise ValueError("Live native session inputs changed after checkpoint")
    elif directory.exists() and any(directory.iterdir()):
        actual, kind = tree_hash(directory), "live_session_inputs"
    else:
        records = []
        for path in (attempt / "native-summaries").glob("*.json"):
            session = json.loads(path.read_text())
            sid = session["session_id"]
            if Path(sid).name != sid or sid in {r[0][:-5] for r in records}:
                raise ValueError("Ambiguous archived native session identity")
            compact = {"session_id": sid, "task_id": session.get("task_id", ""),
                "num_turns": session.get("num_turns", len(session.get("turns", []))),
                "aggregate": session.get("aggregate"), "_skills_referenced": sorted(session.get("_skills_referenced") or []),
                "_avg_prm": session.get("_avg_prm"), "_has_tool_errors": session.get("_has_tool_errors", False),
                "_trajectory": session.get("_trajectory", ""), "_summary": session.get("_summary", "")}
            raw = json.dumps(compact, ensure_ascii=False, indent=2).encode()
            records.append([sid + ".json", sha(raw), 0o644])
        if not records:
            raise ValueError("Native session inputs missing after cleanup without archived summaries")
        actual, kind = sha(json.dumps(sorted(records), sort_keys=True).encode()), "exact_official_compact_projection"
    if actual != expected:
        raise ValueError("Native input bytes differ from the recorded checkpoint")
    return {"method": kind, "sha256": actual, "no_files_reconstructed_or_written": True}


def completed_per_skill_validation(attempt: Path, bank: Path, config: dict) -> dict:
    """Prove genuine native completion; the collector must still check each bundle."""
    if config.get("native_legacy_origin"):
        if not per_skill(config) or config.get("native_checkpoint_origin"):
            raise ValueError("Legacy migration requires its exclusive per-skill origin binding")
        from scripts.injection.legacy_native_receipt import verify_legacy_receipt
        return verify_legacy_receipt(attempt, bank, config)
    origin = config.get("native_checkpoint_origin")
    if origin:
        point_file = Path(origin["checkpoint"])
        if point_file.is_dir():
            point_file = point_file / "checkpoint.json"
        if sha(point_file.read_bytes()) != origin["checkpoint_sha256"]:
            raise ValueError("Native origin checkpoint changed")
        source_protocol = origin["protocol_hash"]
    else:
        outcomes = list((attempt / "native-flow").glob("*/outcome.json"))
        if len(outcomes) != 1:
            raise ValueError("Native completion needs one authoritative invocation")
        outcome = json.loads(outcomes[0].read_text())
        if outcome.get("returncode") not in (0, 78):
            raise ValueError("Unfinished native run cannot publish any partial skill")
        if outcome.get("returncode") == 78 and outcome.get("reason") != "native_loader_failed_after_bounded_feedback":
            raise ValueError("Unrecognized legacy native failure")
        point_file = Path(outcome["checkpoint"]) / "checkpoint.json"
        source_protocol = config["protocol_hash"]
    point = point_file.parent.resolve()
    if not point.is_relative_to((attempt / "native-flow").resolve()):
        raise ValueError("Native origin checkpoint escaped its actual attempt")
    checkpoint = json.loads(point_file.read_text())
    workspace, home = attempt / "workspace", attempt / "openclaw-home"
    sid = checkpoint["session_id"]
    raw = assert_session(session_file(home, sid), sid)
    quiet_path = home / "agent-quiescence.json"
    quiet = json.loads(quiet_path.read_text())
    final = [json.loads(line).get("message", {}) for line in raw.splitlines() if line.strip()]
    final = [message for message in final if message.get("role")]
    assistants = [m for m in final if m.get("role") == "assistant"]
    preserve_links = checkpoint.get("native_candidate_scope") == PER_SKILL_SCOPE
    if (checkpoint.get("returncode") != 0 or checkpoint.get("protocol_hash") != source_protocol
            or checkpoint.get("policy") != POLICY or checkpoint.get("agent_quiescent") is not True
            or Path(checkpoint["workspace"]).resolve() != workspace.resolve()
            or Path(checkpoint["openclaw_home"]).resolve() != home.resolve()
            or checkpoint["transcript_sha256"] != sha(raw) or checkpoint["transcript_bytes"] != len(raw)
            or (point / "transcript.jsonl").read_bytes() != raw
            or quiet.get("quiescent") is not True or quiet.get("surviving_writers") != []
            or quiet.get("agent_exit_code", 0) != 0 or quiet.get("deadline_expired") is True
            or (point / "agent-quiescence.json").read_bytes() != quiet_path.read_bytes()
            or checkpoint["skills_sha256"] != tree_hash(workspace / "skills", preserve_symlinks=preserve_links)
            or tree_hash(point / "skills", preserve_symlinks=preserve_links) != checkpoint["skills_sha256"]
            or checkpoint["parent_bank_sha256"] != tree_hash(bank)
            or not final or final[-1].get("role") != "assistant"
            or not assistants or assistants[-1].get("stopReason") != "stop"
            or not any(c.get("type") == "text" and c.get("text", "").strip() for c in assistants[-1].get("content", []))):
        raise ValueError("Native checkpoint is incomplete, changed or lacks a real terminal answer")
    input_proof = checkpoint_input_proof(attempt, point, checkpoint["session_inputs_sha256"])
    report_path = point / "native-load-validation.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else None
    return {"checkpoint": str(point_file), "checkpoint_sha256": sha(point_file.read_bytes()),
        "native_returncode": 0, "native_session_id": sid, "source_protocol_hash": source_protocol,
        "workspace": str(workspace), "input_proof": input_proof, "report": report,
        "native_checkpoint_origin_explicit": bool(origin), "native_text_or_summary_reexecuted": False}


def _source_evidence_repair_guidance(report: dict) -> dict:
    """Clarify only a genuine failed source check; never supply replacement IDs."""
    if report.get("passed") is not False:
        return {}
    if report.get("native_candidate_scope") == PER_SKILL_SCOPE:
        checks = [bundle.get("source_evidence", {}) for bundle in report.get("bundles", [])
                  if bundle.get("status") == "mechanically_invalid"]
    else:
        checks = [report.get("source_evidence", {})]
    if not any(isinstance(check, dict) and check.get("passed") is False and check.get("error")
               for check in checks):
        return {}
    return {"source_evidence_repair_guidance": {
        "policy": "exact-day-source-identity-repair-guidance-v1",
        "source_directory": "sessions/",
        "instructions": [
            "Read the actual JSON files in sessions/ and copy complete session_id or source_session_id values exactly, including every hash suffix. Do not abbreviate, infer an ID from a filename prefix, or invent a replacement identity.",
            "Every Day reference in every changed history/*_evidence.md is validated, including revision notes, explanations, and examples. Remove invalid references rather than quoting them again to describe the error.",
            "Use only the actual supplied Day evidence for each change. Correct or revert unverifiable changes yourself; this guidance does not relax source checks or provide extra feedback calls or time."
        ]}}


def validation_feedback(report_path: Path, workspace: Path) -> str:
    """Expose the exact report bytes; put only failures and its address in chat."""
    raw = report_path.read_bytes()
    report = json.loads(raw)
    digest = sha(raw)
    directory = workspace / ".native-validation-evidence"
    if directory.is_symlink():
        raise ValueError("Native validation evidence directory is a symlink")
    directory.mkdir(exist_ok=True)
    readable = directory / (digest + ".json")
    if readable.is_symlink():
        raise ValueError("Native validation evidence report is a symlink")
    if readable.exists():
        if readable.read_bytes() != raw:
            raise ValueError("Native validation evidence bytes changed")
    else:
        with readable.open("xb") as stream:
            stream.write(raw)
    if report.get("native_candidate_scope") == PER_SKILL_SCOPE:
        failures = []
        for bundle in report.get("bundles", []):
            if bundle.get("status") != "mechanically_invalid":
                continue
            checks = [("bundle", bundle), ("native_loader", bundle.get("native_loader", {})),
                      ("source_evidence", bundle.get("source_evidence", {}))]
            for name, check in checks:
                if check.get("error"):
                    error = str(check["error"])
                    failures.append({"bundle": bundle["name"], "check": name, "error": error[:256],
                                     "error_is_excerpt": len(error) > 256})
        return json.dumps({"passed": report.get("passed"), "native_candidate_scope": PER_SKILL_SCOPE,
            "eligible_bundle_names": report.get("eligible_bundle_names", []), "errors": failures[:16],
            "additional_error_entries_in_full_report": max(0, len(failures) - 16),
            "full_report": {"path": str(readable), "sha256": digest},
            **_source_evidence_repair_guidance(report)}, ensure_ascii=False)
    errors = []
    for name, check in (("validation", report), ("native_loader", report.get("native_loader", {})),
                        ("source_evidence", report.get("source_evidence", {}))):
        if isinstance(check, dict) and check.get("error"):
            error = str(check["error"])
            item = {"check": name, "error": error[:2048]}
            if len(error) > 2048:
                item.update(error_is_excerpt=True, full_error_characters=len(error))
            errors.append(item)
    return json.dumps({"passed": report.get("passed"), "errors": errors,
                       "full_report": {"path": str(readable), "sha256": digest},
            **_source_evidence_repair_guidance(report)}, ensure_ascii=False)


def _infrastructure_preflight_failure(report: dict) -> tuple[str, str] | None:
    """Find an unknown native-loader failure without treating it as Skill feedback."""
    loaders = [report.get("native_loader", {})]
    loaders.extend(bundle.get("native_loader", {}) for bundle in report.get("bundles", []))
    for loader in loaders:
        if not isinstance(loader, dict) or loader.get("unknown") is not True:
            continue
        error_type, error = str(loader.get("error_type", "")), str(loader.get("error", ""))
        if loader.get("infrastructure_signature") and error:
            return error_type, error
    return None


def rejected_atomic_proposal(attempt: Path, bank: Path, config: dict) -> dict | None:
    """Recognize a real completed native run whose whole library failed checks."""
    from scripts.wildclawbench.protocol import tree_hash as bank_hash

    if per_skill(config):
        return None
    outcomes = list((attempt / "native-flow").glob("*/outcome.json"))
    def mechanical_failure(outcome):
        return (outcome.get("status") == "native_validation_failed"
                and outcome.get("returncode") == 0 and outcome.get("native_validation_passed") is False
                or outcome.get("status") == "recovery_action_required" and outcome.get("returncode") == 78
                and outcome.get("reason") == "native_loader_failed_after_bounded_feedback")

    rejected = [path for path in outcomes if mechanical_failure(json.loads(path.read_text()))]
    if not rejected:
        return None
    if len(outcomes) != 1 or len(rejected) != 1:
        raise ValueError("Atomic rejection lacks one unambiguous native invocation")
    outcome_path = rejected[0]
    outcome = json.loads(outcome_path.read_text())
    point = Path(outcome["checkpoint"]).resolve()
    if point.parent != outcome_path.parent.resolve():
        raise ValueError("Atomic rejection points outside its native invocation")
    invocation = json.loads((outcome_path.parent / "invocation.json").read_text())
    checkpoint = json.loads((point / "checkpoint.json").read_text())
    report_path = point / "native-load-validation.json"
    report = json.loads(report_path.read_text())
    workspace = attempt / "workspace"
    transcript = session_file(Path(checkpoint["openclaw_home"]), checkpoint["session_id"])
    raw = assert_session(transcript, checkpoint["session_id"])
    quiet_path = Path(checkpoint["openclaw_home"]) / "agent-quiescence.json"
    quiet = json.loads(quiet_path.read_text())
    if (not mechanical_failure(outcome) or checkpoint.get("returncode") != 0 or checkpoint.get("policy") != POLICY
            or invocation.get("policy") != POLICY or invocation.get("protocol_hash") != config["protocol_hash"]
            or checkpoint.get("protocol_hash") != config["protocol_hash"]
            or checkpoint.get("session_id") != invocation.get("session_id")
            or Path(checkpoint["workspace"]).resolve() != workspace.resolve()
            or not Path(checkpoint["openclaw_home"]).resolve().is_relative_to(attempt.resolve())
            or checkpoint.get("transcript_sha256") != sha(raw) or checkpoint.get("transcript_bytes") != len(raw)
            or (point / "transcript.jsonl").read_bytes() != raw
            or checkpoint.get("skills_sha256") != tree_hash(workspace / "skills")
            or tree_hash(point / "skills") != checkpoint["skills_sha256"]
            or checkpoint.get("session_inputs_sha256") != tree_hash(workspace / "sessions")
            or checkpoint.get("parent_bank_sha256") != tree_hash(bank)
            or quiet.get("quiescent") is not True or quiet.get("surviving_writers")
            or (point / "agent-quiescence.json").read_bytes() != quiet_path.read_bytes()
            or report.get("passed") is not False or report.get("protocol_hash") != config["protocol_hash"]
            or report.get("candidate_bank_sha256") != bank_hash(workspace / "skills")
            or report.get("parent_bank_sha256") != bank_hash(bank)):
        raise ValueError("Atomic rejection differs from its real completed native checkpoint")
    record = {"name": "native-night-changeset", "action": "atomic_changeset", "bundles": [],
        "reason": "native_validation_failed", "validation_status": "rejected",
        "source_task_ids": [], "source_session_ids": [], "source_evidence": [],
        "source_bank_sha256": bank_hash(bank), "candidate_bank_sha256": bank_hash(workspace / "skills"),
        "raw_candidate_bank": str(workspace / "skills"), "checkpoint": str(point),
        "native_validation_report": str(report_path), "native_validation_report_sha256": sha(report_path.read_bytes()),
        "protocol_hash": config["protocol_hash"], "atomic_change_set_rejected": True}
    record["candidate_id"] = sha(json.dumps(record, sort_keys=True).encode())[:24]
    return record


def run_validated(delegate, workspace: Path, home: Path, message: str, config: dict,
                  session_id: str, journal: Path, *, validate=None, monotonic=time.monotonic):
    """Run one bounded session and feed genuine loader failures to that session.

    All feedback calls share the original 1800-second deadline. A timeout never
    triggers an automatic new allowance or a fresh evolution workspace.
    """
    if config.get("native_night_flow") != POLICY:
        raise ValueError("Native loader feedback must be an explicit frozen protocol")
    if validate is None:
        validate = lambda _bank, _candidate, conf: mechanical_validation(workspace, conf)
    limit = int(config["evolution_timeout_seconds"])
    from scripts.injection import resilient_runner
    resilient = resilient_runner.enabled(config)
    if int(config.get("agent_timeout", limit)) != limit or limit <= 0:
        raise ValueError("Native Night Agent and evolution timeouts must share one positive allowance")
    if not resilient and limit != 1800:
        raise ValueError("This revision preserves the explicit 1800-second Night allowance")
    prior = float(config.get("native_night_prior_elapsed_seconds", 0))
    if not math.isfinite(prior) or not 0 <= prior < limit:
        raise ValueError("Invalid previously consumed native Night allowance")
    if resilient and prior:
        raise ValueError("resilient-v1 resumes from its shared deadline, not a legacy native allowance")
    if not resilient and prior and (config.get("cycle_round") != 2 or config.get("cycle_condition") != "poisoned"):
        raise ValueError("Prior native allowance belongs only to the original poisoned Night2 continuation")
    journal.mkdir(parents=True, exist_ok=False)
    write(journal / "invocation.json", {"policy": POLICY, "session_id": session_id,
        "total_timeout_seconds": limit, "max_validation_feedback_calls": 2,
        "prior_native_elapsed_seconds": prior, "remaining_total_timeout_seconds": limit - prior,
        "protocol_hash": config["protocol_hash"], "fresh_run_once": False,
        "runtime_options": {"tools.loopDetection": {"enabled": True},
                            "thresholds": "installed official defaults", "compaction": "safeguard"}})
    path = session_file(home, session_id)
    prefix = assert_session(path, session_id) if path.exists() else b""
    deadline = monotonic() + limit - prior

    def reject_completed_candidate(result, point, reason):
        write(journal / "outcome.json", {"status": "native_validation_failed", "checkpoint": str(point),
            "returncode": result.returncode, "native_validation_passed": False,
            **({"native_candidate_scope": PER_SKILL_SCOPE,
                "eligible_bundle_names": inspection.get("eligible_bundle_names", []),
                "invalid_bundle_names": inspection.get("invalid_bundle_names", [])} if per_skill(config) else {}),
            "reason": "native_loader_failed_after_bounded_feedback", "feedback_end_reason": reason,
            "source_and_deployment_and_ab_gates_still_required": True})
        return result

    for index in range(3):
        remaining = int(deadline - monotonic())
        if remaining <= 0:
            if index:
                # The preceding completed call already has its failed report.
                return reject_completed_candidate(result, point, "deadline_exhausted")
            return subprocess.CompletedProcess([], 124, "", "Native Night total deadline expired")
        call = {**config, "evolution_timeout_seconds": remaining, "agent_timeout": remaining}
        result = delegate(workspace, home, message, call, session_id)
        assert_session(path, session_id, prefix)
        point = journal / f"segment-{index:02d}"
        record = snapshot(workspace, home, session_id, point, config, result)
        (point / "agent.stdout").write_text(str(result.stdout or ""))
        (point / "agent.stderr").write_text(str(result.stderr or ""))
        prefix = path.read_bytes()
        if result.returncode != 0:
            write(journal / "outcome.json", {"status": "recovery_action_required", "checkpoint": str(point),
                "returncode": result.returncode, "new_native_session": False})
            return result
        before = tree_hash(workspace / "skills", preserve_symlinks=per_skill(config))
        inspection = None
        for preflight_attempt in range(2):
            inspection = validate(
                workspace / "skills", {"candidate_id": "native-night-" + sha(prefix)[:24]}, config)
            infrastructure = _infrastructure_preflight_failure(inspection)
            if infrastructure is None:
                break
            from scripts.injection import resilient_runner
            if not resilient_runner.enabled(config):
                break
            resilient_runner.observe_infrastructure_result(
                journal / "infrastructure-circuit.json",
                f"segment-{index:02d}-preflight-{preflight_attempt}",
                error_type=infrastructure[0], error=infrastructure[1])
        if inspection is None:
            raise RuntimeError("Native candidate preflight produced no result")
        infrastructure = _infrastructure_preflight_failure(inspection)
        if infrastructure is not None and resilient_runner.enabled(config):
            raise RuntimeError(
                "Native candidate infrastructure retry exhausted without scientific feedback: "
                + infrastructure[1])
        if tree_hash(workspace / "skills", preserve_symlinks=per_skill(config)) != before:
            raise ValueError("Mechanical preflight changed the candidate skill bytes")
        write(point / "native-load-validation.json", inspection)
        if inspection.get("passed") is True:
            write(journal / "outcome.json", {"status": "native_loader_passed", "checkpoint": str(point),
                "returncode": 0, "skills_sha256": record["skills_sha256"],
                "source_and_deployment_and_ab_gates_still_required": True})
            return result
        feedback_time_exhausted = int(deadline - monotonic()) <= 0
        if index == 2 or feedback_time_exhausted:
            return reject_completed_candidate(result, point,
                "deadline_exhausted" if feedback_time_exhausted else "feedback_limit_reached")
        if per_skill(config):
            message = ("Mechanical checks found independently invalid skill bundles. Read the actual failures below and full report. "
                "Continue this same session within its existing allowance; correct or revert the named failed bundles yourself. "
                "Keep unrelated verified skills intact. Use exact session/task identities from sessions/. "
                "Do not claim a failed bundle passed; no host-side skill text has been repaired.\n"
                + validation_feedback(point / "native-load-validation.json", workspace))
            continue
        # A real machine result, not an invented score or a host-side text fix.
        message = ("The mechanical native OpenClaw deployment/source check rejected the current atomic skill library. "
            "Read the errors below and the byte-exact full report at the supplied workspace path; continue this same evolution session. Correct the failed loading/resource "
            "checks using the supplied Day evidence, or revert unverifiable changes. Keep the complete change set atomic. "
            "Use the exact session/task IDs visible in sessions/; do not invent or abbreviate source identities. "
            "Do not claim self-validation passed until this mechanical check passes.\n"
            + validation_feedback(point / "native-load-validation.json", workspace))
    raise AssertionError("Unreachable validation feedback state")


def prepare_continuation(source: Path, target: Path, bank: Path, config: dict, *, copytree=shutil.copytree) -> dict:
    """Clone a stopped failed native session; never modify the historical attempt.

    ``copytree`` may be an owning-container read-only copy adapter for root-owned
    native session indices. Failure to read/copy is an error, never reconstruction.
    """
    source, target = source.resolve(), target.resolve()
    if target.exists() or source == target or target.is_relative_to(source):
        raise ValueError("Continuation needs a new separate revision directory")
    if (source.parent.parent.parent / "candidates.json").exists():
        raise ValueError("A finished native proposal must be revalidated, not continued")
    if (source / "candidate.json").exists():
        raise ValueError("A collected native candidate must be revalidated, not continued")
    if config.get("native_night_flow") != POLICY:
        raise ValueError("Continuation must declare the versioned runtime policy")
    quiet = json.loads((source / "openclaw-home/agent-quiescence.json").read_text())
    if quiet.get("quiescent") is not True or quiet.get("surviving_writers"):
        raise ValueError("Historical native writers are not confirmed stopped")
    transcripts = list((source / "openclaw-home/.openclaw/agents/main/sessions").glob("*.jsonl"))
    if len(transcripts) != 1:
        raise ValueError("Failed attempt lacks one unambiguous actual native session")
    header = json.loads(transcripts[0].read_bytes().splitlines()[0])
    session_id = header.get("id")
    prefix = assert_session(transcripts[0], session_id)
    from scripts.wildclawbench.protocol import tree_hash as bank_hash
    parent_hash = bank_hash(bank)
    summary_path = source.parent / "proposal-summary.json"
    previous = None
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text())
        if summary.get("status") != "failed" or Path(summary["attempt_dir"]).resolve() != source:
            raise ValueError("Native continuation requires its exact failed proposal checkpoint")
        input_fingerprint = summary["input_fingerprint"]
        source_protocol = summary["settings"]["protocol_hash"]
        source_bank_hash = summary["source_bank_sha256"]
    else:
        previous = json.loads((source / "continuation.json").read_text())
        outcomes = list((source / "native-flow").glob("*/outcome.json"))
        if len(outcomes) != 1:
            raise ValueError("Continued native session lacks one authoritative failed invocation")
        outcome = json.loads(outcomes[0].read_text())
        point = Path(outcome["checkpoint"]).resolve()
        if (outcome.get("status") != "recovery_action_required" or outcome.get("returncode") != 70
                or point.parent != outcomes[0].parent.resolve()):
            raise ValueError("Continued native session lacks its exact recoverable failed checkpoint")
        checkpoint = json.loads((point / "checkpoint.json").read_text())
        if (Path(previous["continuation_attempt"]).resolve() != source
                or previous["session_id"] != session_id
                or checkpoint.get("returncode") != 70
                or checkpoint.get("session_id") != session_id
                or checkpoint.get("protocol_hash") != previous["continuation_protocol_hash"]
                or checkpoint.get("transcript_sha256") != sha(prefix)
                or checkpoint.get("transcript_bytes") != len(prefix)
                or (point / "transcript.jsonl").read_bytes() != prefix
                or checkpoint.get("skills_sha256") != tree_hash(source / "workspace/skills")
                or tree_hash(point / "skills") != checkpoint["skills_sha256"]
                or checkpoint.get("session_inputs_sha256") != tree_hash(source / "workspace/sessions")
                or checkpoint.get("session_inputs_sha256") != previous["workspace_inputs_sha256"]
                or checkpoint.get("parent_bank_sha256") != tree_hash(bank)
                or Path(checkpoint["workspace"]).resolve() != source / "workspace"
                or Path(checkpoint["openclaw_home"]).resolve() != source / "openclaw-home"
                or (point / "agent-quiescence.json").read_bytes() != (source / "openclaw-home/agent-quiescence.json").read_bytes()):
            raise ValueError("Latest native continuation checkpoint differs from its real stopped state")
        original_prefix = prefix[:previous["transcript_prefix_bytes"]]
        if sha(original_prefix) != previous["transcript_prefix_sha256"]:
            raise ValueError("Continued native session lost its earlier transcript prefix")
        for name in ("raw-sessions", "native-summaries"):
            if tree_hash(source / name) != previous["original_trees"][name]:
                raise ValueError("Continued native session changed its original Day inputs")
        input_fingerprint = previous["original_input_fingerprint"]
        source_protocol = previous["continuation_protocol_hash"]
        source_bank_hash = previous["parent_bank_sha256"]
    if source_bank_hash != parent_hash:
        raise ValueError("Continuation bank differs from the original Day parent")
    originals = {name: tree_hash(source / name) for name in ("workspace", "native-summaries", "raw-sessions")}
    target.mkdir(parents=True)
    for name in ("workspace", "openclaw-home", "native-summaries", "raw-sessions"):
        copytree(source / name, target / name)
    if any(tree_hash(target / name) != digest or tree_hash(source / name) != digest
           for name, digest in originals.items()):
        raise ValueError("Copy adapter changed the stopped native workspace or original Day inputs")
    if previous is not None:
        ancestry = target / "continuation-ancestry"
        ancestry.mkdir()
        copytree(source / "native-flow", ancestry / "native-flow")
        for filename in ("continuation.json", "initial-validation.json"):
            raw = (source / filename).read_bytes()
            (ancestry / filename).write_bytes(raw)
        if tree_hash(ancestry / "native-flow") != tree_hash(source / "native-flow"):
            raise ValueError("Copy adapter changed the previous native invocation evidence")
        if (source / "continuation-ancestry").is_dir():
            copytree(source / "continuation-ancestry", ancestry / "earlier")
    new_home = target / "openclaw-home"
    new_transcript = session_file(new_home, session_id)
    if new_transcript.read_bytes() != prefix:
        raise ValueError("Copy adapter changed the original native transcript")
    index = new_home / ".openclaw/agents/main/sessions/sessions.json"
    registry = json.loads(index.read_text())
    matches = [entry for entry in registry.values() if entry.get("sessionId") == session_id]
    if len(matches) != 1:
        raise ValueError("Native session index does not bind the failed session")
    matches[0]["sessionFile"] = str(new_transcript)
    for key in ("skillsSnapshot", "systemPromptReport"):
        matches[0].pop(key, None)  # Rebuild path-sensitive loader caches in the copied workspace.
    index.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    locks = []
    for lock in sorted(new_transcript.parent.glob("*.lock")):
        preserved = target / "copied-stale-locks" / lock.name
        preserved.parent.mkdir(exist_ok=True)
        locks.append({"path": lock.name, "sha256": sha(lock.read_bytes()),
                      "reason": "original writer subtree confirmed quiescent"})
        shutil.move(str(lock), str(preserved))
    record = {"policy": POLICY, "status": "prepared_not_executed", "session_id": session_id,
        "original_attempt": str(source), "continuation_attempt": str(target),
        "original_input_fingerprint": input_fingerprint,
        "original_trees": originals,
        "parent_bank_sha256": source_bank_hash,
        "transcript_prefix_sha256": sha(prefix), "transcript_prefix_bytes": len(prefix),
        "native_session_index_remapped": True, "original_transcript_rewritten": False,
        "copied_stale_locks_archived": locks,
        "workspace_inputs_sha256": tree_hash(target / "workspace/sessions"),
        "original_protocol_hash": source_protocol,
        "continuation_protocol_hash": config["protocol_hash"],
        "future_days_must_run_after_actual_publication": True}
    if previous is not None:
        record.update(ancestral_original_attempt=previous.get("ancestral_original_attempt", previous["original_attempt"]),
                      previous_native_flow_sha256=tree_hash(source / "native-flow"),
                      previous_failed_checkpoint=str(point),
                      previous_continuation_sha256=sha((source / "continuation.json").read_bytes()))
    write(target / "continuation.json", record)
    return record


def continue_native(delegate, attempt: Path, bank: Path, config: dict, message: str, *, validate=None):
    """Explicitly continue a prepared copy; collection/export remains separate."""
    record = json.loads((attempt / "continuation.json").read_text())
    if record["status"] != "prepared_not_executed" or record["continuation_protocol_hash"] != config["protocol_hash"]:
        raise ValueError("Continuation belongs to another protocol or was already consumed")
    sid = record["session_id"]
    path = session_file(attempt / "openclaw-home", sid)
    raw = path.read_bytes()
    if len(raw) != record["transcript_prefix_bytes"] or sha(raw) != record["transcript_prefix_sha256"]:
        raise ValueError("Prepared continuation transcript changed before execution")
    if tree_hash(attempt / "workspace/sessions") != record["workspace_inputs_sha256"]:
        raise ValueError("Prepared continuation Day inputs changed")
    from scripts.wildclawbench.protocol import tree_hash as bank_hash
    if bank_hash(bank) != record["parent_bank_sha256"]:
        raise ValueError("Prepared continuation parent bank changed")
    prepared_config = {**config, "_proposal_source_bank": str(bank)}
    before = tree_hash(attempt / "workspace/skills")
    initial = (validate(attempt / "workspace/skills", {"candidate_id": "native-resume-initial-check"}, prepared_config)
               if validate is not None else mechanical_validation(attempt / "workspace", prepared_config))
    if tree_hash(attempt / "workspace/skills") != before:
        raise ValueError("Initial mechanical validation changed the stopped native candidate")
    write(attempt / "initial-validation.json", initial)
    scope_message = "each failed bundle independently" if per_skill(config) else "the entire atomic change set"
    message += ("\nBefore continuing, the actual mechanical loader/source check of your saved candidate reported "
                "the status/errors below. Its byte-exact full report is readable at the supplied workspace path. "
                "Continue the original session without repeating already successful writes. "
                "Use sessions/ to verify exact identities; correct or revert failed checks yourself, preserving "
                + scope_message + ". No historical candidate text was repaired by the host.\n"
                + validation_feedback(attempt / "initial-validation.json", attempt / "workspace"))
    journal = attempt / "native-flow" / ("resume-" + uuid.uuid4().hex)
    return run_validated(delegate, attempt / "workspace", attempt / "openclaw-home", message,
        prepared_config, sid, journal, validate=validate)
