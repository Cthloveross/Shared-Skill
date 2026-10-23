"""Independent grading attempts over an authenticated, immutable Agent output."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil

from scripts.wildclawbench import alignment_snapshot, task_runtime
from scripts.wildclawbench.grading_cache import request_key, successful_response
from scripts.wildclawbench.protocol import canonical_hash, write_json


SCHEMA = "independent-grading-recovery-v1"
OVERRIDES = {"local_judge_enable_thinking", "local_judge_max_tokens", "local_api_base_url",
             "gateway_log_dir", "local_request_budget", "grading_timeout_seconds", "case_timeout_seconds",
             "grading_recovery_max_requests", "log_dir"}


def _read(path):
    return json.loads(Path(path).read_text())


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _ref(path):
    return {"path": str(Path(path).resolve()), "sha256": _sha(path)}


def judge_policy(config):
    if config.get("external_judge"):
        raise ValueError("Independent local recovery does not enable paid judges")
    local = task_runtime.local_judge_settings(config) or {
        "enable_thinking": config.get("enable_thinking", True), "max_tokens": config["max_tokens"]}
    return {"model_id": config["model_id"], "model_revision": config["model_revision"], **local,
        **{key: config.get(key) for key in ("seed", "temperature", "top_p", "top_k", "min_p",
            "presence_penalty", "repetition_penalty", "reasoning_effort", "context_length",
            "grading_timeout_seconds")}}


def _events(source):
    path = source / "judge-api.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _successful_cache(source, config, binding, policy_sha, events):
    """Bind raw grader request fingerprints to the original gateway responses."""
    final = {row["fingerprint"]: row for row in events if row.get("kind") == "model_api"}
    valid = {key: row for key, row in final.items() if row.get("status") == 200 and not row.get("error")}
    entries, refs = {}, []
    evidence = Path(config["gateway_log_dir"]) / binding["run_id"]
    for record_path in sorted(evidence.glob("*/record.json")):
        record = _read(record_path)
        incoming, response = record_path.parent / "incoming.json", record_path.parent / "response.bin"
        if (record.get("status") != "completed" or record.get("http_status") != 200
                or record.get("protocol_hash") != binding["protocol_hash"]
                or record.get("actual_model") != config["model_id"]
                or not incoming.is_file() or not response.is_file()):
            continue
        raw, answer = incoming.read_bytes(), response.read_bytes()
        for fingerprint, event in valid.items():
            if hashlib.sha256(event["url"].encode() + raw).hexdigest() != fingerprint:
                continue
            if not successful_response(200, answer, raw):
                continue
            key = request_key(event["url"], raw, policy_sha)
            entries[key] = {"status": 200, "body": answer.decode(), "source_fingerprint": fingerprint}
            refs.extend(_ref(path) for path in (record_path, incoming, response))
    return entries, list({row["path"]: row for row in refs}.values())


def prepare_recovery(source_output: Path, attempt: Path, overrides: dict) -> dict:
    source, attempt = source_output.resolve(), attempt.resolve()
    if source == attempt or attempt.is_relative_to(source) or source.is_relative_to(attempt):
        raise ValueError("Recovery must use an independent attempt directory")
    if set(overrides) - OVERRIDES:
        raise ValueError("Recovery may change only explicit judge and grading transport settings")
    original = _read(source / "runtime-config.json")
    binding = _read(source / "execution-binding.json")
    execution = _read(source / "execution.json")
    if execution.get("status") != "agent_completed":
        raise ValueError("Recovery requires a completed, sealed Agent execution")
    task_id = execution["task_id"]
    identity = alignment_snapshot.execution_identity(task_id, binding["run_id"], binding, original)
    sealed = alignment_snapshot.verify_agent_output(source, identity)
    recovery = alignment_snapshot.verify_grading_recovery(source, identity)
    if (execution.get("output_snapshot_sha256") != sealed["manifest_sha256"]
            or execution.get("grading_recovery_sha256") != recovery["manifest_sha256"]):
        raise ValueError("Original execution does not bind its Agent/mock snapshots")
    selected = {**original, **overrides}
    selected.pop("stage_budget", None)  # This new attempt has its own case/grading hard limits.
    selected.pop("log_dir", None)
    selected["log_dir"] = str(attempt / "logs")
    selected["grade_recovery"] = {"attempt": str(attempt)}
    maximum = selected.get("grading_recovery_max_requests")
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("Specify grading_recovery_max_requests for this independent allowance")
    old_policy, policy = judge_policy(original), judge_policy(selected)
    policy_sha = canonical_hash(policy)
    changed = old_policy != policy
    events = _events(source)
    result_path = source / "runtime-result.json"
    source_result = _read(result_path) if result_path.exists() else {}
    score = source_result.get("score")
    deterministic = (source_result.get("status") == "completed" and type(score) in (int, float)
        and math.isfinite(score) and 0 <= score <= 1
        and not any(row.get("kind") == "model_api" for row in events))
    if source_result.get("scoring_parse_failures"):
        entries, gateway_refs = {}, []  # Ambiguous grader parsing does not certify a successful judgment.
    elif not changed:
        entries, gateway_refs = _successful_cache(source, original, binding, policy_sha, events)
    else:
        entries, gateway_refs = {}, []
    if attempt.exists():
        prior = verify_recovery(attempt, require_result=False)
        if prior["source_output"] != str(source) or prior["judge_policy_sha256"] != policy_sha:
            raise ValueError("Existing grading attempt belongs to another source/policy")
        if _read(attempt / "runtime-config.json") != selected:
            raise ValueError("Existing grading attempt configuration changed")
        return prior
    attempt.mkdir(parents=True)
    shutil.copytree(source / "workspace", attempt / "workspace", symlinks=True,
                    ignore=lambda path, names: {"gt"} if Path(path) == source / "workspace" else set())
    for row in sealed["files"]:
        relative = Path(row["path"])
        if relative.parts[0] == "workspace" or row["kind"] == "directory":
            continue
        target = attempt / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    originals = ["agent-output-manifest.json", "grading-recovery-manifest.json", "execution.json",
                 "mock-replay.json"]
    for name in originals:
        if (source / name).exists():
            shutil.copy2(source / name, attempt / name)
    write_json(attempt / "runtime-config.json", selected)
    write_json(attempt / "model-replay.json", {"schema": "grader-model-replay-v1",
        "judge_policy_sha256": policy_sha, "max_new_requests": maximum, "entries": entries})
    names = ["runtime-config.json", "execution-binding.json", "agent-output-manifest.json",
             "grading-recovery-manifest.json", "execution.json", "mock-replay.json", "runtime-result.json",
             "judge-api.jsonl", "raw-score.json"]
    references = [_ref(source / name) for name in names if (source / name).exists()]
    receipt = {"schema": SCHEMA, "status": "prepared", "task_id": task_id,
        "source_output": str(source), "source_run_id": binding["run_id"],
        "scoring_run_id": "wcb-" + hashlib.sha256(str(attempt).encode()).hexdigest()[:20],
        "source_runtime_config_sha256": _sha(source / "runtime-config.json"),
        "source_execution_binding_sha256": _sha(source / "execution-binding.json"),
        "source_agent_manifest_sha256": _sha(source / "agent-output-manifest.json"),
        "source_recovery_manifest_sha256": _sha(source / "grading-recovery-manifest.json"),
        "source_result_sha256": _sha(result_path) if result_path.exists() else None,
        "copied_agent_manifest_sha256": _sha(attempt / "agent-output-manifest.json"),
        "agent_snapshot_sha256": sealed["manifest_sha256"], "source_references": references + gateway_refs,
        "judge_policy": policy, "judge_policy_sha256": policy_sha, "source_judge_policy": old_policy,
        "judge_policy_changed": changed, "full_rejudge": changed and not deterministic,
        "deterministic_reuse": deterministic, "cached_successful_requests": len(entries),
        "max_new_requests": maximum,
        "model_cache_sha256": _sha(attempt / "model-replay.json"),
        "runtime_config_sha256": _sha(attempt / "runtime-config.json"), "new_agent_executions": 0}
    write_json(attempt / "grade-recovery.json", receipt)
    verify_recovery(attempt, require_result=False)
    return receipt


def verify_recovery(attempt: Path, *, require_result: bool = True) -> dict:
    attempt = attempt.resolve()
    receipt = _read(attempt / "grade-recovery.json")
    if receipt.get("schema") != SCHEMA or receipt.get("new_agent_executions") != 0:
        raise ValueError("Unknown grading recovery contract")
    source = Path(receipt["source_output"])
    for ref in receipt["source_references"]:
        if _sha(Path(ref["path"])) != ref["sha256"]:
            raise ValueError("Original grading/Agent evidence changed")
    original = _read(source / "runtime-config.json")
    binding = _read(source / "execution-binding.json")
    for field, name in (("source_runtime_config_sha256", "runtime-config.json"),
                        ("source_execution_binding_sha256", "execution-binding.json"),
                        ("source_agent_manifest_sha256", "agent-output-manifest.json"),
                        ("source_recovery_manifest_sha256", "grading-recovery-manifest.json")):
        if receipt[field] != _sha(source / name):
            raise ValueError("Original evidence reference changed: " + field)
    if receipt["source_run_id"] != binding["run_id"]:
        raise ValueError("Recovery refers to another Agent execution")
    identity = alignment_snapshot.execution_identity(receipt["task_id"], binding["run_id"], binding, original)
    for root in (source, attempt):
        alignment_snapshot.verify_agent_output(root, identity)
        alignment_snapshot.verify_grading_recovery(root, identity)
    if (_sha(attempt / "agent-output-manifest.json") != receipt["copied_agent_manifest_sha256"]
            or receipt["copied_agent_manifest_sha256"] != receipt["source_agent_manifest_sha256"]
            or _sha(attempt / "runtime-config.json") != receipt["runtime_config_sha256"]
            or _sha(attempt / "model-replay.json") != receipt["model_cache_sha256"]
            or canonical_hash(judge_policy(_read(attempt / "runtime-config.json"))) != receipt["judge_policy_sha256"]):
        raise ValueError("Independent grading attempt binding changed")
    if require_result:
        result = attempt / receipt.get("result_path", "missing-result")
        if (receipt.get("status") != "completed" or result.parent != attempt
                or _sha(result) != receipt.get("result_sha256")):
            raise ValueError("Grading recovery has no authenticated completed result")
        value = _read(result)
        score = value.get("score")
        if (value.get("task_id") != receipt["task_id"] or value.get("run_id") != receipt["scoring_run_id"]
                or value.get("status") != "completed" or type(score) not in (int, float)
                or not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError("Grading recovery result is not a valid task score")
        for ref in receipt.get("result_evidence", []):
            if _sha(attempt / ref["path"]) != ref["sha256"]:
                raise ValueError("Independent grading evidence changed")
    return receipt


def run_recovery(attempt: Path) -> dict:
    attempt = attempt.resolve()
    receipt = verify_recovery(attempt, require_result=False)
    if receipt["status"] == "completed":
        verify_recovery(attempt)
        return _read(attempt / receipt["result_path"])
    if receipt["status"] != "prepared":
        raise ValueError("Grading attempt already started; use a new attempt for another allowance")
    config = _read(attempt / "runtime-config.json")
    receipt["status"] = "running"
    write_json(attempt / "grade-recovery.json", receipt)
    if receipt["deterministic_reuse"]:
        result = _read(Path(receipt["source_output"]) / "runtime-result.json")
        result.update(run_id=receipt["scoring_run_id"], rescore=False, deterministic_reuse=True)
        write_json(attempt / "runtime-result.json", result)
    else:
        binding = _read(attempt / "execution-binding.json")
        result = task_runtime.execute_task(receipt["task_id"], Path(binding["bank_dir"]), attempt, config)
    result.update(grade_recovery=str(attempt / "grade-recovery.json"), new_agent_executions=0)
    write_json(attempt / "experiment-result.json", result)
    receipt.update(status="completed" if result.get("status") == "completed" else "unknown",
        result_path="experiment-result.json", result_sha256=_sha(attempt / "experiment-result.json"))
    receipt["result_evidence"] = [{"path": name, "sha256": _sha(attempt / name)}
        for name in ("runtime-result.json", "raw-score.json", "judge-api.jsonl", "model-responses.json")
        if (attempt / name).exists()]
    write_json(attempt / "grade-recovery.json", receipt)
    verify_recovery(attempt, require_result=receipt["status"] == "completed")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--attempt", required=True, type=Path)
    parser.add_argument("--overrides", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    prepared = prepare_recovery(args.source, args.attempt, _read(args.overrides))
    result = prepared if args.prepare_only else run_recovery(args.attempt)
    print(json.dumps({key: result.get(key) for key in ("status", "task_id", "score", "new_agent_executions")}))


if __name__ == "__main__":
    main()
