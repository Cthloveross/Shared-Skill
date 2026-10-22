"""Version a method correction without replacing historical runs or proposals."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path
import shutil

from scripts.injection import e2e_30 as cohort

from scripts.injection.source_evidence_v2 import POLICY, resolve_history_sources, scoped_history_sources

e2e = cohort.e2e
REVISION = "method-v2.1"


def verify_run_inputs(original: dict) -> None:
    if e2e.sha256(Path(original["frozen_payload"]).read_bytes()) != original["payload_sha256"]:
        raise ValueError("The original frozen payload changed")
    manifest = Path(original["bootstrap_day_run"]) / "finalized-day-inputs/manifest.json"
    if e2e.sha256(manifest.read_bytes()) != original["bootstrap_manifest_sha256"]:
        raise ValueError("The original bootstrap manifest changed")
    *_, tree_hash, _ = e2e.load_components()
    cohort.validate_resume(Path(original["source_run"]), original["settings"], tree_hash)


def verify_revision_sources(root: Path, sources: list[dict]) -> None:
    expected = {f"{index:03d}-{Path(record['path']).name}": record for index, record in enumerate(sources)}
    snapshots = {path.name: path for path in (root / "frozen-source").glob("*")}
    if set(snapshots) != set(expected):
        raise ValueError("The revision source snapshot is incomplete")
    for name, record in expected.items():
        if (e2e.sha256(snapshots[name].read_bytes()) != record["sha256"]
                or e2e.sha256(Path(record["path"]).read_bytes()) != record["sha256"]):
            raise ValueError("The revision source bytes changed")


def stage_root(run: Path, config: dict) -> Path:
    if not config.get("method_revision"):
        return run
    expected = run / "revisions" / REVISION
    if config["method_revision"] != REVISION or Path(config["stage_root"]) != expected:
        raise ValueError("Unknown method revision or stage location")
    return expected


def prepare(run: Path, sources: list[dict]) -> dict:
    """A new immutable protocol references the original completed Night1 calls."""
    from scripts.injection.cycle_validation import build_validation_binding

    original_path = run / "cycles-config.json"
    original = e2e.read_json(original_path)
    if not original:
        raise ValueError("The original cycle protocol is missing")
    verify_run_inputs(original)
    for record in sources:
        if e2e.sha256(Path(record["path"]).read_bytes()) != record["sha256"]:
            raise ValueError("The proposed revision source bytes changed")
    snapshots = sorted((run / "frozen-orchestrator").glob("*"))
    if len(snapshots) != len(original["sources"]):
        raise ValueError("The historical orchestrator snapshot is incomplete")
    for snapshot, record in zip(snapshots, original["sources"], strict=True):
        if e2e.sha256(snapshot.read_bytes()) != record["sha256"]:
            raise ValueError("The historical orchestrator snapshot changed")
    origins, bundles = {}, []
    for condition in original["conditions"]:
        branch = run / "rounds/01" / condition / "branches" / condition
        summary = branch / "nights/01/proposals/evolution/proposal-summary.json"
        candidates = branch / "nights/01/candidates.json"
        publication = branch / "nights/01/publication.json"
        saved = e2e.read_json(summary, {})
        if saved.get("status") != "completed" or not candidates.is_file():
            raise ValueError("Both original Night1 native proposals must finish before revision")
        proposals = saved.get("candidates", []) + saved.get("quarantines", [])
        if len(proposals) > 1:
            raise ValueError("Expected one atomic native changeset per Night")
        for candidate in proposals:
            bundles.extend(b["bundle_dir"] for b in candidate["bundles"])
        optional = [publication, branch / "night-error.json", branch / "night-state.json",
                    branch.parent.parent / "cycle-error.json"]
        artifacts = [summary, candidates, *[path for path in optional if path.is_file()],
                     *sorted((branch.parent.parent / "inputs/sessions").glob("*.json"))]
        origins[condition] = {"branch": str(branch), "original_publication_present": publication.is_file(),
                             "artifacts": [
            {"path": str(path), "sha256": e2e.sha256(path.read_bytes())} for path in artifacts]}
    settings = copy.deepcopy(original["settings"])
    settings["method_revision"] = REVISION
    settings["alignment"]["source_evidence_revision"] = POLICY
    settings["cohort_validation_binding"] = build_validation_binding(Path(original["source_run"]))
    config = {**original, "settings": settings, "sources": sources, "method_revision": REVISION,
              "stage_root": str(run / "revisions" / REVISION), "original_protocol": {
                  "path": str(original_path), "sha256": e2e.sha256(original_path.read_bytes())},
              "native_night1_origins": origins, "imported_bundle_dirs": sorted(set(bundles)),
              "validation_binding_sha256": cohort.digest(settings["cohort_validation_binding"])}
    config["prior_method_revisions"] = [
        {"path": str(path), "sha256": e2e.sha256(path.read_bytes())}
        for path in sorted((run / "revisions").glob("*/cycles-config.json"))
        if path.parent.name != REVISION]
    root = stage_root(run, config)
    path = root / "cycles-config.json"
    previous = e2e.read_json(path)
    if previous and previous != config:
        raise ValueError("The method revision protocol or original evidence changed")
    if not previous:
        e2e.write_json(path, config)
        for index, record in enumerate(sources):
            dest = root / "frozen-source" / f"{index:03d}-{Path(record['path']).name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(record["path"], dest)
        for name in ("state.json", "runner.json", "heartbeat.json"):
            source = run / name
            if source.exists():
                shutil.copy2(source, root / f"historical-{name}")
        e2e.write_json(root / "revision.json", {"created_at": e2e.now(), "revision": REVISION,
            "original_protocol": config["original_protocol"],
            "original_rounds_are_immutable": True, "new_native_night1_calls": 0,
            "night1_action": "Revalidate unchanged completed proposals under the new protocol",
            "fixes": ["per-user sequential Day scheduling", "actual inner cycle round",
                      "exact real source-ID union", "cohort frozen-input validation for A/B",
                      "wait for gateway TIME_WAIT release between completed phases"],
            "remaining_variants": ["local Qwen judges", "shared mixed-protocol Day1",
                "whole-night atomic changesets", "repeated five-task post-Day edits",
                f"S{config.get('rounds', 3)}-only sandbox observations; no full final quality endpoint"]})
    verify_revision_sources(root, sources)
    return config


def verify_originals(config: dict) -> None:
    records = [config["original_protocol"], *config.get("prior_method_revisions", []), *[
        item for origin in config["native_night1_origins"].values() for item in origin["artifacts"]]]
    for record in records:
        if e2e.sha256(Path(record["path"]).read_bytes()) != record["sha256"]:
            raise ValueError(f"Original method evidence changed: {record['path']}")
    verify_run_inputs(e2e.read_json(Path(config["original_protocol"]["path"])))
    verify_revision_sources(Path(config["stage_root"]), config["sources"])


@contextmanager
def execution_context(config: dict, controller, evolution):
    """Scope the new source parser and frozen-input bridge to this revision."""
    from scripts.injection.cycle_validation import validation_adapter

    verify_originals(config)
    with scoped_history_sources(evolution, controller, Path(config["stage_root"]),
                                imported_bundle_dirs=config["imported_bundle_dirs"]):
        with validation_adapter(controller, config["settings"]["cohort_validation_binding"]):
            yield


def seed_night1(stage: Path, settings: dict, config: dict, condition: str, evolution, tree_hash) -> None:
    """Re-resolve evidence, then use the normal deployment/A-B gate on exact bytes."""
    from scripts.wildclawbench.protocol import alignment_candidate_id
    from sharedskill.skill_bundle import read_skill_bundle, read_skill_bundle_with_meta

    verify_originals(config)
    original = Path(config["native_night1_origins"][condition]["branch"])
    source_summary = original / "nights/01/proposals/evolution/proposal-summary.json"
    summary = e2e.read_json(source_summary)
    bank = stage / "banks/S0"
    if tree_hash(bank) != summary["source_bank_sha256"]:
        raise ValueError("Imported native proposal belongs to a different parent bank")
    source_bank = {p.name: read_skill_bundle(p) for p in bank.iterdir() if (p / "SKILL.md").is_file()}
    raw_sessions = [e2e.read_json(p) for p in sorted((original.parent.parent / "inputs/sessions").glob("*.json"))]
    sessions = [evolution._normalize_session(s, set(source_bank), restrict_to_bank=False) for s in raw_sessions]
    proposals = summary.get("candidates", []) + summary.get("quarantines", [])
    candidates, quarantines = [], []
    for saved in proposals:
        candidate = copy.deepcopy(saved)
        for bundle in candidate["bundles"]:
            path = Path(bundle["bundle_dir"])
            if (tree_hash(path) != bundle["bundle_sha256"]
                    or read_skill_bundle_with_meta(path)[2] != bundle["native_tree_sha256"]):
                raise ValueError("The original private candidate bytes changed")
        try:
            selected, evidence = resolve_history_sources(candidate["bundles"], source_bank, sessions)
        except ValueError as error:
            candidate.update(source_task_ids=[], source_session_ids=[], source_evidence=[],
                             reason="unverifiable_sources", source_error=str(error))
            candidate["candidate_id"] = alignment_candidate_id(candidate, settings)
            quarantines.append(candidate)
        else:
            candidate.pop("source_error", None)
            candidate.pop("reason", None)
            candidate.update(source_task_ids=sorted({s["task_id"] for s in selected}),
                source_session_ids=sorted(s["source_session_id"] for s in selected), source_evidence=evidence,
                source_outcomes=[{"task_id": s["task_id"], "session_id": s["source_session_id"],
                                  "outcome": s.get("outcome", {})} for s in selected])
            candidate["candidate_id"] = alignment_candidate_id(candidate, settings)
            candidates.append(candidate)
    night = stage / "branches" / condition / "nights/01"
    proposed = {"status": "completed", "source_bank_sha256": summary["source_bank_sha256"],
        "candidates": candidates, "quarantines": quarantines, "reused_native_proposal": True,
        "new_native_calls": 0, "original_summary": str(source_summary),
        "original_summary_sha256": e2e.sha256(source_summary.read_bytes()),
        "source_evidence_revision": POLICY}
    for path, value in ((night / "candidates.json", candidates),
                        (night / "proposals/evolution/proposal-summary.json", proposed)):
        if path.exists():
            if e2e.read_json(path) != value:
                raise ValueError("Previously seeded proposal changed")
        else:
            e2e.write_json(path, value)
    e2e.write_json(stage / "native-proposal-import.json", {"original_branch": str(original),
        "candidate_cache_sha256": e2e.sha256((night / "candidates.json").read_bytes()),
        "new_native_calls": 0, "sources_re_resolved": True,
        "original_inputs_sha256": cohort.digest(sorted(raw_sessions, key=lambda s: s["session_id"]))})


def verify_night1_inputs(stage: Path, sessions: list[dict]) -> None:
    imported = e2e.read_json(stage / "native-proposal-import.json")
    if not imported or imported["original_inputs_sha256"] != cohort.digest(sorted(sessions, key=lambda s: s["session_id"])):
        raise ValueError("Revalidation inputs differ from the original completed native proposal")
