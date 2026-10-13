"""Independent native Agent Evolver proposals; this module never publishes a bank.

``propose_candidates(round_dir, bank_dir, sessions, config)`` returns JSON-compatible
candidate records. ``bundle_sha256`` uses the experiment's path-and-bytes hash;
``native_tree_sha256`` additionally records SharedSkill's native bundle hash.
The controller must validate and publish the exact exported bytes separately.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import os
import posixpath
import re
import subprocess
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from evolve_server.core.config import EvolveServerConfig
from evolve_server.core.llm_client import AsyncLLMClient
from evolve_server.core.utils import parse_skill_content
from evolve_server.engines.agent import AgentEvolveServer
from evolve_server.pipeline.aggregation import aggregate_sessions_by_skill
from evolve_server.pipeline.summarizer import _extract_session_metadata, summarize_sessions_parallel
from sharedskill.skill_bundle import bundle_file_records, bundle_tree_sha256, read_skill_bundle, write_skill_bundle


class EvolutionError(RuntimeError):
    """A proposal run could not complete or violated its independent edit scope."""


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _tree_hash(directory: Path) -> str:
    # This is the experiment protocol's immutable path-and-bytes hash, rather
    # than the different native SharedSkill manifest hash.
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise EvolutionError(f"Symlink in proposal or source bank: {path}")
        if path.is_file():
            relative = path.relative_to(directory).as_posix().encode()
            data = path.read_bytes()
            digest.update(len(relative).to_bytes(8, "big") + relative)
            digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def _safe_id(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", value)[:70].strip("-") or "group"
    return f"{slug}-{hashlib.sha256(value.encode()).hexdigest()[:12]}"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _normalize_skill_path(path: str) -> str:
    """Match OpenClaw's container-root home expansion without host filesystem IO."""
    expanded = path.replace("\\", "/")
    if expanded.startswith("~/"):
        expanded = "/root/" + expanded[2:]
    return posixpath.normpath(expanded)


def _bank_source_name(path: str, bank_names: set[str]) -> str | None:
    """An image skill with the same basename is not the shared bank copy."""
    parts = PurePosixPath(_normalize_skill_path(path)).parts
    if len(parts) == 5 and parts[:3] == ("/", "root", "skills"):
        name, filename = parts[3:]
    elif len(parts) == 3 and parts[0] == "skills":
        name, filename = parts[1:]
    else:
        return None
    return name if name in bank_names and filename == "SKILL.md" else None


def _restrict_bank_references(session: dict, bank_names: set[str]) -> None:
    """Keep complete source metadata, but only shared bundles can be refined."""
    for turn in session.get("turns", []):
        for field in ("read_skills", "modified_skills", "injected_skills"):
            original = turn.get(field) or []
            retained = []
            for item in original:
                name = str(item.get("skill_name", item.get("name", "")) if isinstance(item, dict) else item).strip()
                path = item.get("path", item.get("file_path")) if isinstance(item, dict) else None
                if not path and name.endswith("/SKILL.md"):
                    path = name
                path_name = _bank_source_name(str(path), bank_names) if path else None
                if (path and path_name) or (not path and name in bank_names):
                    if isinstance(item, dict) and path:
                        record = copy.deepcopy(item)
                        record.update(skill_name=path_name, path=_normalize_skill_path(str(path)))
                        record.setdefault("original_path", str(path))
                    elif path:
                        record = path_name
                    else:
                        record = item
                    retained.append(record)
            if retained != original:
                turn.setdefault("source_" + field, copy.deepcopy(original))
                turn[field] = retained
    _extract_session_metadata(session)
    session["_skills_referenced"] = sorted(set(session["_skills_referenced"]) & bank_names)


def _messages_to_turns(messages: list[dict], bank_names: set[str]) -> list[dict]:
    """Convert actual OpenClaw/OpenAI message records, never inferred answers."""
    turns: list[dict] = []
    call_turns: dict[str, dict] = {}
    call_info: dict[str, dict] = {}
    instruction = ""
    for entry in messages:
        message = entry.get("message", entry)
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            instruction = _content_text(message.get("content", ""))
        elif role == "assistant":
            turn = {
                "turn_num": len(turns) + 1,
                "prompt_text": instruction,
                "response_text": _content_text(message.get("content", "")),
                "tool_calls": [],
                "tool_results": [],
                "read_skills": [],
            }
            calls = list(message.get("tool_calls") or [])
            content = message.get("content")
            if isinstance(content, list):
                calls.extend(
                    item
                    for item in content
                    if isinstance(item, dict) and item.get("type") in {"toolCall", "tool_use", "tool_call"}
                )
            for index, call in enumerate(calls):
                function = call.get("function", call)
                arguments = function.get("arguments", function.get("input", {}))
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"raw_arguments": arguments}
                call_id = str(call.get("id", f"turn-{len(turns) + 1}-call-{index}"))
                normalized = {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": str(function.get("name", "")),
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
                turn["tool_calls"].append(normalized)
                call_turns[call_id] = turn
                call_info[call_id] = {"name": function.get("name", ""), "arguments": arguments}
            turns.append(turn)
        elif role in {"tool", "toolResult"}:
            call_id = str(message.get("tool_call_id", message.get("toolCallId", "")))
            turn = call_turns.get(call_id)
            if turn is None:
                continue
            error = bool(message.get("isError", message.get("is_error", False)))
            turn["tool_results"].append(
                {
                    "tool_call_id": call_id,
                    "tool_name": message.get("toolName", message.get("name", "")),
                    "content": _content_text(message.get("content", "")),
                    "has_error": error,
                }
            )
            if error:
                turn.setdefault("tool_errors", []).append(turn["tool_results"][-1])
            info = call_info[call_id]
            tool_name = str(info["name"]).lower()
            arguments = info["arguments"]
            command = str(arguments.get("command", "")) if isinstance(arguments, dict) else ""
            reads_file = tool_name in {"read", "read_file", "readfile"} or (
                tool_name in {"exec", "execute", "bash", "shell", "run_command"}
                and bool(re.search(r"(?:^|[;&|\n])\s*(?:cat|head|tail|sed|less|more)\s", command))
            )
            if not error and reads_file:
                argument_text = json.dumps(info["arguments"], ensure_ascii=False)
                for match in re.finditer(r"[~a-zA-Z0-9_./\\-]+[/\\]SKILL\.md", argument_text):
                    source_path = match[0]
                    normalized_path = _normalize_skill_path(source_path)
                    name = _bank_source_name(source_path, bank_names)
                    provenance = {
                        "path": normalized_path,
                        "original_path": source_path,
                        "name": PurePosixPath(normalized_path).parent.name,
                        "environment_or_shared": "shared" if name else "environment",
                    }
                    if provenance not in turn.setdefault("actual_skill_reads", []):
                        turn["actual_skill_reads"].append(provenance)
                    if name:
                        record = {"skill_name": name, "path": normalized_path, "original_path": source_path}
                        if record not in turn["read_skills"]:
                            turn["read_skills"].append(record)
    return turns


def _normalize_session(raw: dict, bank_names: set[str]) -> dict:
    session = copy.deepcopy(raw)
    task_id = str(session.get("task_id", "")).strip()
    source_id = str(session.get("session_id", "")).strip()
    if not task_id or not source_id:
        raise EvolutionError("Every day session requires an explicit task_id and session_id")
    if session.get("phase") != "day":
        raise EvolutionError(f"Only daytime evidence may enter evolution: {source_id}")
    if not isinstance(session.get("turns"), list):
        messages = session.get("messages", session.get("transcript"))
        if isinstance(messages, (str, Path)):
            transcript_path = Path(messages)
            if not transcript_path.is_file():
                raise EvolutionError(f"Missing real transcript for {source_id}")
            messages = [json.loads(line) for line in transcript_path.read_text().splitlines() if line.strip()]
        if not isinstance(messages, list):
            raise EvolutionError(f"Session {source_id} has neither native turns nor real messages")
        session["turns"] = _messages_to_turns(messages, bank_names)
    if not session["turns"]:
        raise EvolutionError(f"Session {source_id} has no recorded agent turns")
    for turn in session["turns"]:
        errors = turn.get("tool_errors")
        if errors is not None and not isinstance(errors, list):
            turn["source_tool_error_indicator"] = errors
            turn["tool_errors"] = (
                [{"tool_name": "", "content": f"Recorded source error indicator: {errors}"}] if errors else []
            )
    session["source_session_id"] = source_id
    session["session_id"] = _safe_id(source_id)
    session["num_turns"] = len(session["turns"])
    outcome = session.get("outcome") or {}
    if not isinstance(outcome, dict):
        raise EvolutionError(f"Invalid real outcome in {source_id}")
    aggregate = dict(session.get("aggregate") or {})
    aggregate["outcome"] = outcome
    for field in ("user_id", "phase", "round", "skill_bank_sha256"):
        if field in session:
            aggregate[field] = session[field]
    if isinstance(outcome.get("score"), (int, float)):
        aggregate["mean_score"] = outcome["score"]
    if isinstance(outcome.get("success"), bool):
        aggregate.update(
            success_count=int(outcome["success"]),
            fail_count=int(not outcome["success"]),
            stability="all_success" if outcome["success"] else "all_fail",
        )
    session["aggregate"] = aggregate
    # Cached fields supplied by a caller are not evidence of native ingestion.
    for field in ("_summary", "_trajectory", "_skills_referenced"):
        session.pop(field, None)
    _restrict_bank_references(session, bank_names)
    return session


def _configuration(config: dict | Any) -> dict:
    values = dict(config) if isinstance(config, dict) else vars(config).copy()
    values["llm_base_url"] = values.get("llm_base_url", values.get("local_api_base_url", "http://127.0.0.1:18182/v1"))
    values["llm_model"] = values.get("llm_model", values.get("model_id", ""))
    values["llm_max_tokens"] = int(values.get("llm_max_tokens", values.get("max_tokens", 16384)))
    values["llm_temperature"] = float(values.get("llm_temperature", values.get("temperature", 1.0)))
    values["agent_timeout"] = int(values.get("agent_timeout", values.get("evolution_timeout_seconds", 1800)))
    parsed = urlparse(values["llm_base_url"])
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
    ):
        raise EvolutionError("Zero-budget proposal inference requires a loopback HTTP endpoint")
    if not values["llm_model"] or (
        int(values.get("paid_api_budget_usd", 0)) != 0 and values.get("external_judge") is not True
    ):
        raise EvolutionError("An explicit local model is required; paid budget is allowed only for an external judge")
    return values


class _BoundedSummarizer:
    def __init__(self, client: AsyncLLMClient, config: dict) -> None:
        self.client, self.config = client, config
        self.semaphore = asyncio.Semaphore(max(1, int(config.get("evolution_summary_concurrency", 2))))

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        # Native summarizer requests 100000 output tokens; respect the frozen
        # experiment limit rather than silently allowing a larger GPU request.
        kwargs["max_tokens"] = min(
            int(kwargs.get("max_tokens", self.config["llm_max_tokens"])), self.config["llm_max_tokens"]
        )
        kwargs["temperature"] = self.config["llm_temperature"]
        async with self.semaphore:
            return await self.client.chat(messages, **kwargs)


class _CachedNativeEvolver(AgentEvolveServer):
    async def _summarize_sessions(self, sessions: list[dict]) -> None:
        if not all(session.get("_summary") and session.get("_trajectory") for session in sessions):
            raise EvolutionError("Group evidence must have completed the native summarizer")


class _RecordingRunner:
    def __init__(self, delegate: Any, config: dict, group_dir: Path, home: Path) -> None:
        self.delegate, self.config, self.group_dir, self.home = delegate, config, group_dir, home
        self.returncode: int | None = None

    def run(
        self, workspace_path: Path, message: str, session_id: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        source_bank = Path(self.config["_proposal_source_bank"])
        for path in source_bank.rglob("*"):
            if path.is_file():
                copied = workspace_path / "skills" / path.relative_to(source_bank)
                if copied.is_file():
                    copied.chmod(path.stat().st_mode & 0o777)
        result = self.delegate(workspace_path, self.home, message, self.config, session_id or uuid.uuid4().hex)
        self.returncode = result.returncode
        self.group_dir.joinpath("agent.stdout.txt").write_text(str(result.stdout or ""))
        self.group_dir.joinpath("agent.stderr.txt").write_text(str(result.stderr or ""))
        _write_json(
            self.group_dir / "agent-process.json",
            {
                "returncode": result.returncode,
                "elapsed_seconds": time.monotonic() - started,
                "model": self.config["llm_model"],
                "session_id": session_id,
            },
        )
        if result.returncode != 0:
            # Native server otherwise collects and uploads partial edits even
            # when the OpenClaw subprocess failed. Abort before that step.
            raise EvolutionError(f"Native evolver failed with returncode {result.returncode}; see {self.group_dir}")
        _tree_hash(workspace_path / "skills")
        return result


def _runner_delegate(config: dict) -> Any:
    injected = config.get("evolution_runner")
    if injected is not None:
        return injected
    try:
        return importlib.import_module("environment").run_evolver
    except (ImportError, AttributeError) as exc:
        raise EvolutionError("environment.run_evolver must provide the isolated real OpenClaw runtime") from exc


def _prompt(group: str, source_task_ids: list[str], reserved_environment_skill_names: list[str] | None = None) -> str:
    native = Path(__file__).resolve().parents[3] / "evolve_server/engines/EVOLVE_AGENTS.md"
    base = native.read_text()
    return (
        base
        + "\n\n"
        + "# Independent experimental proposal scope\n\n"
        + json.dumps(
            {
                "evidence_group": group,
                "source_task_ids": source_task_ids,
                "allowed_existing_skill_edit": None if group == "__no_skill__" else group,
                "maximum_changed_or_new_skill_bundles": 1,
                "reserved_environment_skill_names_for_new_skills": sorted(reserved_environment_skill_names or []),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n\n"
        + (
            "This is a private proposal run, not publication or task A/B validation.\n"
            "All other existing skill bundles are read-only context. Make at most ONE proposal: "
            "refine the allowed existing skill, OR create one distinct new skill, OR skip. "
            "Do not rename/delete existing skills or remove their supporting files. "
            "For create, choose a name absent from both the current shared bank and the reserved environment names; "
            "an existing allowed shared-bank target may still be refined even if its name is reserved. "
            "Preserve successful procedures while correcting failures supported by these sessions. "
            "The aggregate.outcome fields are actual daytime results; do not invent scores. "
            "For the no-skill group, only create one new skill or skip. "
            "If making a proposal, write proposal-rationale.json in the workspace root with "
            '{"name": "skill-name", "action": "refine|create", "reason": "evidence-based explanation", '
            '"source_task_ids": ["explicit task IDs from the supplied evidence"]}. '
            "The controller, separately, will run the real toolchain A/B test.\n"
        )
    )


def _changed_files(before: dict[str, bytes], after: dict[str, bytes]) -> list[dict]:
    return [
        {
            "path": path,
            "change": "added" if path not in before else "removed" if path not in after else "modified",
            "before_sha256": hashlib.sha256(before[path]).hexdigest() if path in before else None,
            "after_sha256": hashlib.sha256(after[path]).hexdigest() if path in after else None,
        }
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    ]


async def _propose(round_dir: Path, bank_dir: Path, sessions: list[dict], config: dict) -> list[dict]:
    started = time.monotonic()
    bank_hash = _tree_hash(bank_dir)
    bank = {
        path.name: read_skill_bundle(path)
        for path in sorted(bank_dir.iterdir())
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    if not bank:
        raise EvolutionError("The deployed bank has no skill bundles")
    normalized = [_normalize_session(session, set(bank)) for session in sessions]
    if len({session["source_session_id"] for session in normalized}) != len(normalized):
        raise EvolutionError("Duplicate source session IDs in one night")
    declared_bank_hashes = {
        session.get("skill_bank_sha256") for session in normalized if session.get("skill_bank_sha256")
    }
    if declared_bank_hashes and declared_bank_hashes != {bank_hash}:
        raise EvolutionError("Daytime evidence does not belong to the frozen source bank")
    settings = {
        key: config[key] for key in ("llm_model", "llm_base_url", "llm_max_tokens", "llm_temperature", "agent_timeout")
    }
    for key in (
        "protocol_hash",
        "seed",
        "top_p",
        "top_k",
        "min_p",
        "presence_penalty",
        "repetition_penalty",
        "reasoning_effort",
        "context_length",
        "model_revision",
        "benchmark_revision",
        "dataset_revision",
    ):
        settings[key] = config.get(key)
    settings["evolution_summary_concurrency"] = int(config.get("evolution_summary_concurrency", 2))
    fingerprint = _hash(
        {
            "bank_hash": bank_hash,
            "sessions": normalized,
            "settings": settings,
            "native_prompt_sha256": hashlib.sha256(
                _prompt("__no_skill__", [], config.get("environment_skill_names")).encode()
            ).hexdigest(),
        }
    )
    output = round_dir / "evolution"
    summary_path = output / "proposal-summary.json"
    if summary_path.is_file():
        saved = json.loads(summary_path.read_text())
        if saved.get("status") == "completed" and saved.get("input_fingerprint") == fingerprint:
            for candidate in saved["candidates"]:
                if _tree_hash(Path(candidate["bundle_dir"])) != candidate["bundle_sha256"]:
                    raise EvolutionError("Cached exported candidate bytes changed")
            return saved["candidates"]
    attempt = output / ("attempt-" + uuid.uuid4().hex[:12])
    attempt.mkdir(parents=True)
    record = {
        "status": "running",
        "input_fingerprint": fingerprint,
        "source_bank_sha256": bank_hash,
        "settings": settings,
        "attempt_dir": str(attempt),
        "candidates": [],
        "groups": [],
    }
    _write_json(summary_path, record)
    try:
        raw_archive = attempt / "raw-sessions"
        for original, normalized_session in zip(sessions, normalized):
            _write_json(raw_archive / f"{normalized_session['session_id']}.json", original)
        summary_started = time.monotonic()
        if normalized:
            client = AsyncLLMClient(
                api_key=str(config.get("local_api_key", config.get("llm_api_key", "local-experiment"))),
                base_url=config["llm_base_url"],
                model=config["llm_model"],
                max_tokens=config["llm_max_tokens"],
                temperature=config["llm_temperature"],
            )
            await summarize_sessions_parallel(_BoundedSummarizer(client, config), normalized)
            if any(not session.get("_summary") for session in normalized):
                raise EvolutionError("A native session summary failed; refusing incomplete evidence")
        record["summarization_elapsed_seconds"] = time.monotonic() - summary_started
        # Avoid native aggregation's set-valued metadata in persisted JSON.
        for session in normalized:
            _restrict_bank_references(session, set(bank))
            session["_skills_referenced"] = sorted(session["_skills_referenced"])
            _write_json(attempt / "native-summaries" / f"{session['session_id']}.json", session)
        groups = aggregate_sessions_by_skill(normalized)
        runner = _runner_delegate(config) if groups else None
        for group, group_sessions in sorted(groups.items()):
            # Native NO_SKILL_KEY is imported lazily to avoid depending on its
            # spelling in the stable external candidate contract.
            from evolve_server.core.constants import NO_SKILL_KEY

            scope = "__no_skill__" if group == NO_SKILL_KEY else group
            if scope != "__no_skill__" and scope not in bank:
                raise EvolutionError(f"Evidence referenced a skill outside the deployed bank: {scope}")
            group_dir = attempt / "groups" / _safe_id(scope)
            group_dir.mkdir(parents=True)
            task_ids = sorted({session["task_id"] for session in group_sessions})
            session_ids = sorted(session["source_session_id"] for session in group_sessions)
            prompt_path = group_dir / "EVOLVE_AGENTS.md"
            prompt_path.write_text(_prompt(scope, task_ids, config.get("environment_skill_names")))
            native_config = EvolveServerConfig(
                engine="agent",
                storage_backend="local",
                local_root=str(group_dir / "private-store"),
                group_id="proposal",
                llm_base_url=config["llm_base_url"],
                llm_model=config["llm_model"],
                llm_api_key=str(config.get("local_api_key", config.get("llm_api_key", "local-experiment"))),
                llm_max_tokens=config["llm_max_tokens"],
                llm_temperature=config["llm_temperature"],
                openclaw_home=str(group_dir / "openclaw-home"),
                workspace_root=str(group_dir / "workspace"),
                agents_md_path=str(prompt_path),
                fresh=True,
                agent_timeout=config["agent_timeout"],
                history_path=str(group_dir / "native-history.jsonl"),
            )
            engine = _CachedNativeEvolver(native_config)
            for name, bundle in bank.items():
                engine._upload_skill(parse_skill_content(name, bundle["SKILL.md"].decode()), bundle, "seed")
            for session in group_sessions:
                engine._bucket.put_object(
                    f"proposal/sessions/{session['session_id']}.json", json.dumps(session, ensure_ascii=False).encode()
                )
            runner_config = {**config, "_proposal_source_bank": str(bank_dir)}
            recorder = _RecordingRunner(runner, runner_config, group_dir, Path(native_config.openclaw_home))
            engine._runner = recorder
            group_started = time.monotonic()
            native_result = await engine.run_once()
            _write_json(group_dir / "native-result.json", native_result)
            workspace = Path(native_config.workspace_root)
            final = {
                path.name: read_skill_bundle(path)
                for path in (workspace / "skills").iterdir()
                if path.is_dir() and (path / "SKILL.md").is_file()
            }
            _tree_hash(workspace / "skills")
            if set(bank) - set(final):
                raise EvolutionError(f"Proposal group {scope} deleted an existing skill")
            changed = [name for name in final if name not in bank or final[name] != bank[name]]
            if any(name in bank and name != scope for name in changed) or len(changed) > 1:
                raise EvolutionError(f"Proposal group {scope} edited outside its single-candidate scope: {changed}")
            group_record = {
                "evidence_group": scope,
                "source_task_ids": task_ids,
                "source_session_ids": session_ids,
                "elapsed_seconds": time.monotonic() - group_started,
                "artifact_dir": str(group_dir),
                "status": "skip",
            }
            for name in changed:
                before, after = bank.get(name, {}), final[name]
                if set(before) - set(after):
                    raise EvolutionError(f"Proposal {name} removed supporting files")
                changes = _changed_files(before, after)
                if not any(not item["path"].startswith("history/") for item in changes):
                    group_record["status"] = "history_only_skip"
                    continue
                exported = group_dir / "candidate" / name
                write_skill_bundle(exported, after, clean=True)
                for source_path in (workspace / "skills" / name).rglob("*"):
                    if source_path.is_file():
                        target_path = exported / source_path.relative_to(workspace / "skills" / name)
                        if target_path.is_file():
                            target_path.chmod(source_path.stat().st_mode & 0o777)
                candidate_hash = _tree_hash(exported)
                rationale_path = workspace / "proposal-rationale.json"
                rationale = json.loads(rationale_path.read_text()) if rationale_path.is_file() else None
                candidate = {
                    "candidate_id": _hash({"group": scope, "name": name, "bank": bank_hash, "bundle": candidate_hash})[
                        :24
                    ],
                    "name": name,
                    "skill_name": name,
                    "action": "refine" if name in bank else "create",
                    "bundle_dir": str(exported),
                    "bundle_sha256": candidate_hash,
                    "base_bundle_sha256": _tree_hash(bank_dir / name) if name in bank else None,
                    "native_tree_sha256": bundle_tree_sha256(after),
                    "source_bank_sha256": bank_hash,
                    "source_skill_name": None if scope == "__no_skill__" else scope,
                    "evidence_group": scope,
                    "source_task_ids": task_ids,
                    "source_session_ids": session_ids,
                    "source_outcomes": [
                        {
                            "task_id": session["task_id"],
                            "session_id": session["source_session_id"],
                            "outcome": session.get("outcome", {}),
                        }
                        for session in group_sessions
                    ],
                    "changed_files": changes,
                    "files": bundle_file_records(after),
                    "rationale": rationale,
                    "proposal_artifact": str(group_dir / "candidate.json"),
                    "validation_status": "not_validated",
                    "engine": "native-agent-private-store",
                }
                _write_json(group_dir / "candidate.json", candidate)
                record["candidates"].append(candidate)
                group_record.update(status="proposed", candidate_id=candidate["candidate_id"])
            record["groups"].append(group_record)
            _write_json(summary_path, record)
        if _tree_hash(bank_dir) != bank_hash:
            raise EvolutionError("The deployed bank changed during proposal generation")
        record.update(status="completed", elapsed_seconds=time.monotonic() - started)
        _write_json(summary_path, record)
        return record["candidates"]
    except Exception as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}", elapsed_seconds=time.monotonic() - started)
        _write_json(summary_path, record)
        raise


def propose_candidates(
    round_dir: str | Path, bank_dir: str | Path, sessions: list[dict], config: dict | Any
) -> list[dict]:
    """Generate independent complete bundles, leaving ``bank_dir`` untouched.

    This blocking controller API requires an isolated ``environment.run_evolver``
    callable; tests may inject one as ``config['evolution_runner']``. A failed
    group raises ``EvolutionError`` and leaves its artifacts for diagnosis.
    Completed identical inputs reuse hash-checked exported proposals.
    """
    return asyncio.run(_propose(Path(round_dir).resolve(), Path(bank_dir).resolve(), sessions, _configuration(config)))
