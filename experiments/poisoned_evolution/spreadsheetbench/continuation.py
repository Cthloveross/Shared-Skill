"""Import immutable attempts into a new recovery run without generating again.

Only observation and error classification may change. Inputs, model requests,
native engine, initial skill and campaign budget must retain their identities.
This module never creates a smoke gate or invokes an inference backend.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import tarfile
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .engine import is_model_output_error
from .evolution import (
    _atomic_json,
    _contract_error,
    _diagnostics,
    _s0_text,
    _select_batch,
    _trial_from_dict,
    _validate_existing_trial,
)
from .layout import resolve_path
from .models import ProtocolError, TrialStatus
from .provenance import sha256_tree
from .transforms import canonical_session_bytes, transform_trial_sessions
from .usage import ledger_path_from_config

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER_ROOT = Path(__file__).resolve().parent


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProtocolError(f"continuation evidence is not an object: {path}")
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_generation_config(source: Mapping[str, Any], target: Mapping[str, Any]) -> None:
    """Reject scientific changes and any resetting or enlargement of the budget."""

    for field in ("protocol_version", "model", "sources", "corpus", "engine", "families", "smoke"):
        if source.get(field) != target.get(field):
            raise ProtocolError(f"continuation changes generation config: {field}")
    for field in ("matrix", "budgets"):
        ignored = {"run_id"} if field == "matrix" else {
            "campaign_carryover_min_requests", "campaign_carryover_min_tokens",
        }
        old = {key: value for key, value in (source.get(field) or {}).items() if key not in ignored}
        new = {key: value for key, value in (target.get(field) or {}).items() if key not in ignored}
        if old != new:
            raise ProtocolError(f"continuation changes generation config: {field}")
    if ledger_path_from_config(source, repository_root=REPO_ROOT) != ledger_path_from_config(
        target, repository_root=REPO_ROOT,
    ):
        raise ProtocolError("continuation must reuse the existing campaign usage ledger")
    old_ids, new_ids = source.get("run_ids") or {}, target.get("run_ids") or {}
    if old_ids.get("formal") == new_ids.get("formal") or old_ids.get("smoke") == new_ids.get("smoke"):
        raise ProtocolError("continuation requires new formal and real smoke run IDs")


def _definition(source: str, name: str, *, through_call: str | None = None) -> str:
    node = next((item for item in ast.parse(source).body if getattr(item, "name", None) == name), None)
    if node is None:
        raise ProtocolError(f"cannot verify generation definition: {name}")
    if through_call is not None:
        selected = []
        for statement in node.body:
            selected.append(statement)
            if any(
                isinstance(child, ast.Await) and isinstance(child.value, ast.Call)
                and isinstance(child.value.func, ast.Attribute) and child.value.func.attr == through_call
                for child in ast.walk(statement)
            ):
                node.body = [item for item in selected if not isinstance(item, (ast.Import, ast.ImportFrom))]
                break
        else:
            raise ProtocolError(f"cannot verify native generation boundary: {name}")
    return ast.dump(node, include_attributes=False)


def validate_generation_code(source_artifact: Path) -> None:
    """Compare generation code, permitting post-generation classification fixes."""

    archive_path = source_artifact / "source/poisoned-evolution-spreadsheetbench-driver.tar.gz"
    with tarfile.open(archive_path, "r:gz") as archive:
        def frozen(name: str) -> str:
            stream = archive.extractfile(f"driver/{name}")
            if stream is None:
                raise ProtocolError(f"frozen generation source missing: {name}")
            return stream.read().decode("utf-8")

        for name in ("transforms.py", "corpus.py", "corpus_import.py"):
            if frozen(name) != (DRIVER_ROOT / name).read_text(encoding="utf-8"):
                raise ProtocolError(f"continuation changes trajectory generation source: {name}")
        old, new = frozen("engine.py"), (DRIVER_ROOT / "engine.py").read_text(encoding="utf-8")
        for name in ("RecordingBoundedLLM", "_inference_controls", "_retryable"):
            if _definition(old, name) != _definition(new, name):
                raise ProtocolError(f"continuation changes model request source: {name}")
        old = frozen("_e9c_bridge.py")
        new = (DRIVER_ROOT / "_e9c_bridge.py").read_text(encoding="utf-8")
        if _definition(old, "_run", through_call="run_once") != _definition(new, "_run", through_call="run_once"):
            raise ProtocolError("continuation changes native run_once inputs or configuration")


def _copy_once(source: Path, target: Path) -> None:
    """Commit a directory atomically; never merge with an unrelated partial copy."""

    if target.exists():
        if sha256_tree(source) != sha256_tree(target):
            raise ProtocolError(f"continuation target differs from preserved source: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".continuation-", dir=target.parent) as temporary:
        staged = Path(temporary) / "trial"
        shutil.copytree(source, staged)
        staged.rename(target)


def prepare_continuation(
    config: Mapping[str, Any],
    *,
    artifact_root: Path,
    output_root: Path,
    manifests: Sequence[Mapping[str, Any]],
    corpus_sessions: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    s0_path: Path,
) -> dict[str, Any] | None:
    """Validate then import every prior attempt, retaining unknowns and their cost.

    Call only after the new run has frozen its sources and passed its own real
    smoke. The returned record separates inherited generation from rescoring.
    """

    options = config.get("continuation")
    if not options:
        return None
    if not isinstance(options, Mapping):
        raise ProtocolError("continuation must be an object")
    source_artifact = resolve_path(str(options["source_artifacts"]), repo_root=REPO_ROOT)
    source_output = resolve_path(str(options["source_outputs"]), repo_root=REPO_ROOT)
    artifact_root, output_root = Path(artifact_root).resolve(), Path(output_root).resolve()
    if source_artifact == artifact_root or source_output == output_root:
        raise ProtocolError("continuation must not overwrite its source run")
    record_path = artifact_root / "continuation-record.json"
    if record_path.exists():
        record = _read(record_path)
        if record.get("source_artifacts") != str(source_artifact) or record.get("source_outputs") != str(source_output):
            raise ProtocolError("continuation source differs from the committed import")
        for item in record["attempts"]:
            relative = Path("trials") / item["family_id"] / item["batch_id"] / "trial-result.json"
            if _digest(source_output / relative) != item["source_result_sha256"]:
                raise ProtocolError("preserved source trial result changed after import")
            if _digest(output_root / relative) != item["imported_result_sha256"]:
                raise ProtocolError("imported trial result changed after import")
        return record

    # Lazy import avoids the CLI -> continuation -> CLI import cycle.
    from .cli import _validate_source_freeze

    old_freeze = _validate_source_freeze(source_artifact)
    new_freeze = _validate_source_freeze(artifact_root)
    old_config = _read(source_artifact / "frozen-config.json")
    validate_generation_config(old_config, config)
    validate_generation_code(source_artifact)
    for key in ("engine_archive_sha256", "evaluator_sha256", "s0_bundle_sha256", "task_selection_sha256", "scoring_contract_sha256"):
        if old_freeze.get(key) != new_freeze.get(key):
            raise ProtocolError(f"continuation changes frozen scientific evidence: {key}")
    if sha256_tree(s0_path) != old_freeze["s0_bundle_sha256"]:
        raise ProtocolError("continuation S0 differs from the preserved source")
    old_manifests = _read(source_artifact / "batch-manifests.json")["batches"]
    if old_manifests[:len(manifests)] != list(manifests):
        raise ProtocolError("continuation changes preregistered batch manifests")
    by_batch = {str(row["batch_id"]): row for row in manifests}
    initial_text = _s0_text(s0_path)
    pending = []
    counts = {"completed": 0, "unknown": 0}
    for source_result in sorted(source_output.glob("trials/*/*/trial-result.json")):
        result = _trial_from_dict(_read(source_result))
        family, batch = result.family_id, result.batch_id
        if family not in config["matrix"]["families"] or batch not in by_batch:
            raise ProtocolError("preserved attempt is outside the continuation matrix")
        relative = Path("trials") / family / batch
        old_artifact = source_artifact / relative
        manifest = by_batch[batch]
        _validate_existing_trial(result, family_id=family, batch_id=batch, manifest=manifest, artifact_trial=old_artifact)
        transformed = transform_trial_sessions(
            _select_batch(corpus_sessions, manifest), manifest["carrier_session_ids"], family,
            batch_size=int(config["matrix"]["batch_size"]), carrier_count=int(config["matrix"]["carrier_count"]),
            protocol_version=str(config["protocol_version"]),
        )
        preserved = json.loads((old_artifact / "transformed-sessions.json").read_text(encoding="utf-8"))
        if canonical_session_bytes(preserved) != canonical_session_bytes(list(transformed.sessions)):
            raise ProtocolError(f"continuation changes actual trial input: {result.trial_id}")
        if json.loads(json.dumps(transformed.evidence.to_dict())) != _read(old_artifact / "transform-evidence.json"):
            raise ProtocolError(f"continuation transformation evidence differs: {result.trial_id}")
        request = _read(old_artifact / "engine/request.json")
        if request.get("sessions") != preserved:
            raise ProtocolError(f"native request differs from preserved input: {result.trial_id}")
        for name, content in result.engine.materialized_skills.items():
            skill_path = old_artifact / "engine/storage/pe-single-round/skills" / name / "SKILL.md"
            if skill_path.read_text(encoding="utf-8") != content:
                raise ProtocolError(f"materialized source differs from recorded final text: {result.trial_id}")
        for session_id, content in result.engine.summaries.items():
            summary_path = source_output / relative / "engine/debug/summarizer" / f"{session_id}_summary.txt"
            if summary_path.read_text(encoding="utf-8") != content:
                raise ProtocolError(f"summary source differs from recorded text: {result.trial_id}")
        counts[result.status.value] += 1
        original_success = result.ser_success
        reclassified = False
        if result.status == TrialStatus.COMPLETED:
            if _contract_error(result.engine, expected_sessions=len(preserved), s0_text=initial_text):
                raise ProtocolError(f"completed source trial violates the native contract: {result.trial_id}")
            diagnostics = _diagnostics(
                family_id=family, transformed_sessions=preserved, carriers=manifest["carrier_session_ids"],
                engine=result.engine, s0_text=initial_text,
            )
            if diagnostics.cross_family_detector_hits:
                raise ProtocolError(f"rescored completed trial has cross-family hits: {result.trial_id}")
            result = replace(result, diagnostics=diagnostics, ser_success=diagnostics.materialized_positive_skills > 0)
        elif is_model_output_error(result.engine.error_type):
            reclassified = result.engine.infrastructure_signature is not None
            result = replace(result, engine=replace(result.engine, infrastructure_signature=None))
        pending.append((relative, result, {
            "family_id": family, "batch_id": batch, "status": result.status.value,
            "source_result_sha256": _digest(source_result), "source_ser_success": original_success,
            "rescored_ser_success": result.ser_success, "model_error_reclassified": reclassified,
        }))
    for status, count in counts.items():
        expected = options.get(f"expected_{status}")
        if expected is not None and count != expected:
            raise ProtocolError(f"continuation expected {expected} {status} attempts, found {count}")
    if not pending:
        raise ProtocolError("continuation source has no committed attempts")
    source_state = _read(source_output / "matrix-state.json")
    if source_state.get("status") == "running":
        raise ProtocolError("continuation source matrix is still running")
    source_matrix = _read(source_output / "matrix-result.json")
    if any(int(source_matrix.get(status, -1)) != count for status, count in counts.items()):
        raise ProtocolError("continuation attempt counts differ from the preserved matrix result")
    if int(source_matrix.get("attempted", -1)) != len(pending):
        raise ProtocolError("continuation must preserve every prior attempted trial")
    ledger = _read(ledger_path_from_config(config, repository_root=REPO_ROOT))
    totals = ledger["totals"]
    for field, observed in (
        ("campaign_carryover_min_requests", int(totals["requests"])),
        ("campaign_carryover_min_tokens", int(totals["tokens_consumed"]) + int(totals["tokens_reserved"])),
    ):
        if int(config["budgets"].get(field, 0)) <= 0 or observed < int(config["budgets"][field]):
            raise ProtocolError(f"continuation campaign usage floor is invalid: {field}")

    attempts = []
    for relative, result, item in pending:
        _copy_once(source_artifact / relative, artifact_root / relative)
        target_output = output_root / relative
        result_path = target_output / "trial-result.json"
        payload = json.loads(json.dumps(result.to_dict()))
        if result_path.exists() and _read(result_path) == payload:
            pass  # A previous import may have stopped before its final record.
        else:
            _copy_once(source_output / relative, target_output)
            _atomic_json(result_path, payload)
        item["imported_result_sha256"] = _digest(result_path)
        attempts.append(item)
    state_path = output_root / "matrix-state.json"
    if not state_path.exists():
        _atomic_json(state_path, {
            "schema_version": 1, "status": "imported", "started_unix": source_state["started_unix"],
            "elapsed_seconds": float(source_state["elapsed_seconds"]), "attempted": len(attempts), **counts,
        })
    record = {
        "schema_version": 1, "source_artifacts": str(source_artifact), "source_outputs": str(source_output),
        "source_run_id": old_config["run_ids"]["formal"], "target_run_id": config["run_ids"]["formal"],
        "generation_reused_without_calls": True, "unknowns_retained_as_attempts": True,
        "smoke_reused": False, "inherited_elapsed_seconds": float(source_state["elapsed_seconds"]),
        "campaign_usage_ledger": str(ledger_path_from_config(config, repository_root=REPO_ROOT)),
        "counts": counts, "attempts": attempts,
    }
    _atomic_json(record_path, record)
    return record
