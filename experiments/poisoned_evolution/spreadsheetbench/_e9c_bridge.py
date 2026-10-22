#!/usr/bin/env python3
"""Internal subprocess bridge for the frozen public SkillClaw checkout.

This file uses only the standard library until the requested checkout has
been placed first on ``sys.path`` and verified.  It is not a public CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("bridge request must be an object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _git_head(checkout: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def _under(root: Path, value: str | Path) -> bool:
    try:
        Path(value).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _xlsx_source(path: Path) -> Path:
    source = path.resolve()
    if source.is_symlink():
        raise ValueError("S0 source may not be a symlink")
    if (source / "SKILL.md").is_file():
        return source
    candidate = source / "xlsx"
    if not candidate.is_symlink() and (candidate / "SKILL.md").is_file():
        return candidate
    raise ValueError(f"S0 does not contain xlsx/SKILL.md: {source}")


def _has_xlsx_read(session: Mapping[str, Any]) -> bool:
    for turn in session.get("turns") or []:
        if not isinstance(turn, Mapping):
            continue
        for item in turn.get("read_skills") or []:
            value = item.get("skill_name") if isinstance(item, Mapping) else item
            if str(value or "").strip() == "xlsx":
                return True
    return False


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    clean = re.sub(r"```(?:json)?\s*", "", str(raw or "").strip()).strip().rstrip("`")
    try:
        value = json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        start, end = clean.find("{"), clean.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(clean[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def _json_object_error(raw: str) -> str:
    """Describe the received bytes without repairing or reinterpreting them."""

    if not str(raw or "").strip():
        return "empty model response"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"JSONDecodeError: {exc.msg}; line {exc.lineno}, column {exc.colno}, character {exc.pos}"
    if not isinstance(value, dict):
        return f"expected a JSON object, received {type(value).__name__}"
    return "native JSON-object parsing failed after its existing normalization"


def _materialized_skills(storage: Path, group_id: str) -> dict[str, str]:
    root = storage / group_id / "skills"
    if not root.is_dir():
        return {}
    result: dict[str, str] = {}
    for entry in sorted(root.iterdir()):
        path = entry / "SKILL.md"
        if entry.is_dir() and path.is_file():
            result[entry.name] = path.read_text(encoding="utf-8")
    return result


async def _run(request: Mapping[str, Any]) -> dict[str, Any]:
    checkout = Path(str(request["engine_checkout"])).resolve()
    repository = Path(str(request["repository_root"])).resolve()
    expected_commit = str(request["expected_commit"])
    actual_commit = _git_head(checkout)
    if actual_commit != expected_commit:
        raise ValueError(f"engine checkout changed: {actual_commit} != {expected_commit}")

    # The frozen engine wins every potentially conflicting import.  The host
    # repository is second and is used only for the version-neutral recorder.
    sys.path.insert(0, str(checkout))
    sys.path.insert(1, str(repository))

    import skillclaw  # type: ignore[import-not-found]
    from skillclaw.skill_hub import SkillHub  # type: ignore[import-not-found]

    import evolve_server  # type: ignore[import-not-found]
    from evolve_server.core.config import EvolveServerConfig  # type: ignore[import-not-found]
    from evolve_server.engines.workflow import EvolveServer  # type: ignore[import-not-found]
    from experiments.poisoned_evolution.spreadsheetbench.engine import RecordingBoundedLLM
    from experiments.poisoned_evolution.spreadsheetbench.usage import UsageLedger

    if not _under(checkout, evolve_server.__file__) or not _under(checkout, skillclaw.__file__):
        raise ValueError("bridge imported SkillClaw outside the frozen checkout")

    work = Path(str(request["work_dir"])).resolve()
    output_work = Path(str(request.get("output_work_dir") or work)).resolve()
    output_work.mkdir(parents=True, exist_ok=True)
    storage = work / "storage"
    local_skills = work / "initial-skills"
    debug = output_work / "debug"
    group_id = str(request.get("group_id") or "pe-single-round")
    sessions = request.get("sessions")
    expected_count = int(request["expected_session_count"])
    if not isinstance(sessions, list) or len(sessions) != expected_count:
        raise ValueError("bridge session count differs from the frozen trial shape")
    if any(not isinstance(session, dict) for session in sessions):
        raise ValueError("bridge sessions must be JSON objects")
    ids = [str(session.get("session_id") or "") for session in sessions]
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value) for value in ids):
        raise ValueError("session_id is empty or unsafe")
    if len(set(ids)) != expected_count:
        raise ValueError("session_ids must be unique")
    if any(not _has_xlsx_read(session) for session in sessions):
        raise ValueError("every native session must record read_skills xlsx")

    source = _xlsx_source(Path(str(request["s0_path"])))
    destination = local_skills / "xlsx"
    destination.parent.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source, destination)

    hub = SkillHub(
        backend="local",
        endpoint="",
        bucket="",
        access_key_id="",
        secret_access_key="",
        local_root=str(storage),
        group_id=group_id,
        user_alias="poisoned-evolution-reproduction",
    )
    pushed = hub.push_skills(str(local_skills))
    if int(pushed.get("uploaded", 0)) != 1 or int(pushed.get("total_local", 0)) != 1:
        raise ValueError(f"S0 push did not create exactly one xlsx skill: {pushed}")

    queue = storage / group_id / "sessions"
    queue.mkdir(parents=True, exist_ok=False)
    for index, session in enumerate(sessions):
        (queue / f"{index:03d}.json").write_text(
            json.dumps(session, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    llm = request.get("llm")
    if not isinstance(llm, Mapping):
        raise ValueError("llm settings are required")
    base_url = str(llm.get("base_url") or "")
    model = str(llm.get("model") or "")
    api_key_env = str(llm.get("api_key_env") or "OPENAI_API_KEY")
    api_key = os.environ.get(api_key_env, "")
    hostname = (urlparse(base_url).hostname or "").casefold()
    if not api_key and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{api_key_env} is unset for a non-loopback provider")
    if not api_key:
        api_key = "local-not-secret"
    if "max_tokens" not in llm:
        raise ValueError("llm.max_tokens is required")
    max_tokens = int(llm["max_tokens"])
    if max_tokens <= 0:
        raise ValueError("llm.max_tokens must be positive")
    chat_template_kwargs = llm.get("chat_template_kwargs")
    if chat_template_kwargs != {"enable_thinking": False}:
        raise ValueError('llm.chat_template_kwargs must be exactly {"enable_thinking": false}')

    config = EvolveServerConfig(
        storage_backend="local",
        local_root=str(storage),
        group_id=group_id,
        llm_api_key=api_key,
        llm_base_url=base_url,
        llm_model=model,
        llm_max_tokens=max_tokens,
        llm_temperature=float(llm.get("temperature", 0.4)),
        evolve_batch_size=expected_count,
        use_session_judge=False,
        use_skill_verifier=False,
        publish_mode="direct",
        skill_reload_mode="off",
        proxy_reload_url="",
        history_path=str(output_work / "evolve-history.jsonl"),
        processed_log_path=str(output_work / "evolve-processed.json"),
        debug_dump_dir=str(debug),
    )
    server = EvolveServer(config)
    usage_path = llm.get("usage_ledger_path")
    usage_ledger = (
        UsageLedger(
            str(usage_path),
            max_requests=int(llm.get("usage_max_requests", 5000)),
            max_tokens=int(llm.get("usage_max_tokens", 200_000_000)),
        )
        if usage_path
        else None
    )
    recorder = RecordingBoundedLLM(
        server._llm,
        log_path=output_work / "llm-calls.jsonl",
        max_tokens=max_tokens,
        chat_template_kwargs=dict(chat_template_kwargs),
        top_p=float(llm["top_p"]) if llm.get("top_p") is not None else None,
        max_concurrency=int(llm.get("summary_concurrency", 8)),
        infrastructure_retries=int(llm.get("infrastructure_retries", 1)),
        max_calls=int(llm.get("request_hard_cap", 32)),
        summary_timeout_seconds=float(llm.get("summary_timeout_seconds", 600)),
        evolution_timeout_seconds=float(llm.get("evolution_timeout_seconds", 1800)),
        usage_ledger=usage_ledger,
        usage_group=str(llm.get("usage_group") or "formal"),
        usage_group_request_limit=(
            int(llm["usage_group_request_limit"]) if llm.get("usage_group_request_limit") is not None else None
        ),
    )
    server._llm = recorder
    summary = await server.run_once()

    ordered_summaries = sorted(
        (record for record in recorder.records if record.stage == "summary"),
        key=lambda record: record.call_index,
    )
    # Summary requests run concurrently.  Recorder call order is useful for
    # accounting, but it is not a durable session-to-response identifier.
    # The native summarizer writes each post-gather result under its validated
    # session ID, so use those artifacts for the scientific stage mapping.
    summary_dir = debug / "summarizer"
    expected_summary_names = {f"{session_id}_summary.txt" for session_id in ids}
    actual_summary_names = {path.name for path in summary_dir.glob("*_summary.txt")} if summary_dir.is_dir() else set()
    summary_artifacts_valid = actual_summary_names == expected_summary_names
    summaries: dict[str, str] = {}
    for session_id in ids:
        path = summary_dir / f"{session_id}_summary.txt"
        summaries[session_id] = path.read_text(encoding="utf-8") if path.is_file() else ""
    candidate_records = sorted(
        (record for record in recorder.records if record.stage in {"evolver", "merge"}),
        key=lambda record: record.call_index,
    )
    raw_candidates = [record.response_text or "" for record in candidate_records if record.response_text is not None]
    evolver = next((record for record in candidate_records if record.stage == "evolver"), None)
    parsed = _parse_json_object(evolver.response_text or "") if evolver else None
    action = str((parsed or {}).get("action") or "")
    materialized = _materialized_skills(storage, group_id)

    error_type: str | None = None
    error_message: str | None = None
    decision = "unknown"
    failed_calls = sorted(
        (record for record in recorder.records if record.error_type),
        key=lambda record: record.call_index,
    )
    if failed_calls:
        # Native summarization may swallow a transport exception and leave an
        # empty summary. Preserve the actual request failure for the breaker.
        failed = failed_calls[0]
        error_type = "ProviderRequestError"
        error_message = f"{failed.stage}: {failed.error_type}: {failed.error_message}"
    elif (
        len(ordered_summaries) != expected_count
        or not summary_artifacts_valid
        or len(summaries) != expected_count
    ):
        error_type = "SummaryArtifactError"
        error_message = "native session summary artifacts are incomplete or mismatched"
    elif any(not text.strip() for text in summaries.values()):
        error_type = "EmptySummary"
        error_message = "model returned an empty session summary without a request failure"
    elif evolver is None or evolver.response_text is None:
        error_type = "NativeProcessingError" if summary.get("had_processing_error") else "MissingEvolverResponse"
        error_message = "native run produced no received evolver response"
    elif parsed is None:
        error_type = "InvalidEvolverOutput"
        error_message = _json_object_error(evolver.response_text)
    elif action not in {"skip", "improve_skill", "optimize_description", "create_skill"}:
        error_type = "InvalidEvolverSchema"
        error_message = f"unsupported or missing native action: {action!r}"
    elif action != "skip" and not isinstance(parsed.get("skill"), dict):
        error_type = "InvalidEvolverSchema"
        error_message = f"native action {action!r} omitted the skill object"
    elif bool(summary.get("had_processing_error")):
        error_type = "NativeProcessingError"
        error_message = "EvolveServer.run_once reported had_processing_error"
    elif action == "skip":
        decision = "skip"
    elif action in {"improve_skill", "optimize_description", "create_skill"}:
        if not summary.get("evolutions") or not any(
            bool(record.get("uploaded")) for record in summary.get("evolutions") or [] if isinstance(record, Mapping)
        ):
            error_type = "MaterializationError"
            error_message = f"native action {action!r} did not publish a materialized skill"
        elif not materialized or any(not text.strip() for text in materialized.values()):
            error_type = "MaterializationError"
            error_message = "native materialized skill set is empty or invalid"
        else:
            decision = "materialized"

    return {
        "engine_commit": actual_commit,
        "engine_source": str(Path(evolve_server.__file__).resolve()),
        "decision": decision,
        "summary": summary,
        "summaries": summaries,
        "raw_candidates": raw_candidates,
        "materialized_skills": materialized,
        "llm_calls": [record.to_dict() for record in sorted(recorder.records, key=lambda item: item.call_index)],
        "error_type": error_type,
        "error_message": error_message,
    }


def main() -> int:
    args = _args()
    request_path = Path(args.request).resolve()
    result_path = Path(args.result).resolve()
    request: dict[str, Any] = {}
    try:
        request = _read_json(request_path)
        result = asyncio.run(_run(request))
    except BaseException as exc:
        traceback.print_exc(file=sys.stderr)
        checkout = Path(str(request.get("engine_checkout") or ".")).resolve()
        result = {
            "engine_commit": str(request.get("expected_commit") or ""),
            "engine_source": str(checkout / "evolve_server" / "__init__.py"),
            "decision": "unknown",
            "summary": {},
            "summaries": {},
            "raw_candidates": [],
            "materialized_skills": {},
            "llm_calls": [],
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
    _write_json(result_path, result)
    return 0 if not result.get("error_type") else 2


if __name__ == "__main__":
    raise SystemExit(main())
