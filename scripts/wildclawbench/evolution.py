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
from evolve_server.pipeline.summarizer import (
    _extract_session_metadata,
    build_session_trajectory,
    summarize_session,
    summarize_sessions_parallel,
)
from sharedskill.skill_bundle import bundle_file_records, bundle_tree_sha256, read_skill_bundle, write_skill_bundle


class EvolutionError(RuntimeError):
    """A proposal run could not complete or violated its independent edit scope."""


TARGET_SCOPE_VERSION = "single-target-skill-batch-v1"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _attach_typed_sources(candidate: dict, config: dict) -> None:
    from scripts.injection import resilient_runner
    if resilient_runner.enabled(config):
        candidate["source_refs"] = resilient_runner.typed_source_refs(
            candidate.get("source_task_ids", []), candidate.get("source_session_ids", []))


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


def _summary_checkpoint_session(session: dict) -> dict:
    """Return a JSON-safe summary snapshot without changing live metadata."""
    snapshot = copy.deepcopy(session)
    skills = snapshot.get("_skills_referenced")
    if isinstance(skills, (set, frozenset)):
        snapshot["_skills_referenced"] = sorted(skills)
    return snapshot


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


def _messages_to_turns(messages: list[dict], bank_names: set[str], include_environment: bool = False) -> list[dict]:
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
            if include_environment:
                details = message.get("details")
                details = details if isinstance(details, dict) else {}
                code = details.get("exitCode")
                failed_statuses = {"error", "failed", "failure", "aborted", "cancelled", "canceled", "timeout"}
                error = error or str(details.get("status", "")).lower() in failed_statuses
                error = error or (code is not None and code not in (0, "0"))
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
            if include_environment:
                from scripts.wildclawbench.alignment_reads import _paths

                paths = _paths({"name": tool_name, "arguments": arguments})
            else:
                argument_text = json.dumps(info["arguments"], ensure_ascii=False)
                paths = [match[0] for match in re.finditer(
                    r"[~a-zA-Z0-9_./\\-]+[/\\]SKILL\.md", argument_text
                )] if reads_file else []
            if not error:
                for source_path in paths:
                    if not source_path.endswith("/SKILL.md"):
                        continue
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
                    if name or include_environment:
                        record = {"skill_name": name or provenance["name"], "path": normalized_path,
                                  "original_path": source_path}
                        if record not in turn["read_skills"]:
                            turn["read_skills"].append(record)
    return turns


def _normalize_session(raw: dict, bank_names: set[str], restrict_to_bank: bool = True) -> dict:
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
        session["turns"] = _messages_to_turns(messages, bank_names, include_environment=not restrict_to_bank)
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
    if restrict_to_bank:
        _restrict_bank_references(session, bank_names)
    else:
        _extract_session_metadata(session)
        session["_skills_referenced"] = sorted(session["_skills_referenced"])
    return session


def _apply_target_evolution_scope(
    sessions: list[dict], bank_names: set[str], config: dict
) -> dict | None:
    """Route one logical record per task to one existing target skill.

    WildClawBench is heterogeneous, unlike PoisonedEvolution's spreadsheet-only
    batch.  This explicit adapter makes that methodological deviation auditable:
    it preserves the skills actually observed in every Day record, then changes
    only the native aggregation key used for this Night proposal.
    """
    raw = config.get("target_evolution_scope")
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("version") != TARGET_SCOPE_VERSION:
        raise EvolutionError("Unknown target_evolution_scope version")
    if config.get("alignment") is not None:
        raise EvolutionError("single-target routing cannot be combined with whole-night alignment")
    target = raw.get("skill_name")
    expected = raw.get("expected_sessions")
    if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", target):
        raise EvolutionError("target_evolution_scope requires a safe skill_name")
    if target not in bank_names:
        raise EvolutionError("target_evolution_scope skill is absent from the parent bank")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
        raise EvolutionError("target_evolution_scope requires a positive expected_sessions")
    if len(sessions) != expected:
        raise EvolutionError(
            f"target_evolution_scope expected {expected} logical records, found {len(sessions)}"
        )
    task_ids = [session.get("task_id") for session in sessions]
    source_ids = [session.get("source_session_id") for session in sessions]
    if (any(not isinstance(value, str) or not value for value in task_ids + source_ids)
            or len(set(task_ids)) != expected or len(set(source_ids)) != expected):
        raise EvolutionError("target_evolution_scope requires one unique session per unique task")

    rows = []
    actual_read_count = 0
    for session in sessions:
        observed = sorted(set(session.get("_skills_referenced", [])))
        actual_read = target in observed
        actual_read_count += int(actual_read)
        session["_observed_skills_referenced"] = observed
        session["_skills_referenced"] = [target]
        session["target_evolution_routing"] = {
            "version": TARGET_SCOPE_VERSION,
            "skill_name": target,
            "actual_target_read_observed": actual_read,
            "aggregation_key_overridden": observed != [target],
        }
        rows.append({
            "task_id": session["task_id"],
            "source_session_id": session["source_session_id"],
            "observed_skills_referenced": observed,
            "actual_target_read_observed": actual_read,
        })
    return {
        "version": TARGET_SCOPE_VERSION,
        "skill_name": target,
        "logical_record_count": expected,
        "unique_task_count": len(set(task_ids)),
        "actual_target_read_count": actual_read_count,
        "routing_is_not_claimed_as_an_observed_read": True,
        "records": rows,
    }


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
        self.failure_lock = asyncio.Lock()
        self.last_infrastructure_signature: str | None = None
        self.consecutive_infrastructure_failures = 0
        self.circuit_error: Exception | None = None

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        # Native summarizer requests 100000 output tokens; respect the frozen
        # experiment limit rather than silently allowing a larger GPU request.
        kwargs["max_tokens"] = min(
            int(kwargs.get("max_tokens", self.config["llm_max_tokens"])), self.config["llm_max_tokens"]
        )
        kwargs["temperature"] = self.config["llm_temperature"]
        async with self.semaphore:
            from scripts.injection import resilient_runner
            if self.circuit_error is not None:
                raise self.circuit_error
            budget = resilient_runner.active_budget(self.config) if resilient_runner.enabled(self.config) else None
            try:
                if budget is None:
                    result = await self.client.chat(messages, **kwargs)
                else:
                    remaining = budget.require_remaining("native-summary-request")
                    result = await asyncio.wait_for(self.client.chat(messages, **kwargs), timeout=remaining)
            except Exception as error:
                signature = resilient_runner.infrastructure_signature(type(error).__name__, str(error))
                if signature is not None:
                    async with self.failure_lock:
                        if signature == self.last_infrastructure_signature:
                            self.consecutive_infrastructure_failures += 1
                        else:
                            self.last_infrastructure_signature = signature
                            self.consecutive_infrastructure_failures = 1
                        if self.consecutive_infrastructure_failures >= 2:
                            self.circuit_error = resilient_runner.CircuitBreakerOpen(
                                "Native summary infrastructure failed twice with the same signature")
                    if self.circuit_error is not None:
                        raise self.circuit_error from error
                raise
            async with self.failure_lock:
                self.last_infrastructure_signature = None
                self.consecutive_infrastructure_failures = 0
            return result


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
        from scripts.injection import resilient_runner
        run_config = self.config
        if resilient_runner.enabled(self.config):
            budget = resilient_runner.active_budget(self.config)
            if budget is not None:
                run_config = budget.cap_config(self.config, "native-evolver-agent")
        source_bank = Path(self.config["_proposal_source_bank"])
        for path in source_bank.rglob("*"):
            if path.is_file():
                copied = workspace_path / "skills" / path.relative_to(source_bank)
                if copied.is_file():
                    copied.chmod(path.stat().st_mode & 0o777)
        actual_session_id = session_id or uuid.uuid4().hex
        if self.config.get("native_night_flow"):
            from scripts.injection.native_night_flow import run_validated
            result = run_validated(self.delegate, workspace_path, self.home, message, run_config,
                                   actual_session_id, self.group_dir / "native-flow" / ("run-" + uuid.uuid4().hex))
        else:
            result = self.delegate(workspace_path, self.home, message, run_config, actual_session_id)
        self.returncode = result.returncode
        self.group_dir.joinpath("agent.stdout.txt").write_text(str(result.stdout or ""))
        self.group_dir.joinpath("agent.stderr.txt").write_text(str(result.stderr or ""))
        _write_json(
            self.group_dir / "agent-process.json",
            {
                "returncode": result.returncode,
                "elapsed_seconds": time.monotonic() - started,
                "model": self.config["llm_model"],
                "session_id": actual_session_id,
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
        return importlib.import_module("scripts.wildclawbench.environment").run_evolver
    except (ImportError, AttributeError) as exc:
        raise EvolutionError("environment.run_evolver must provide the isolated real OpenClaw runtime") from exc


def _target_only_native_guide(native: str) -> str:
    """Keep the native guide while removing its incompatible creation path."""
    guide = native
    for pattern in (
        r"(?ms)^### create_skill\n.*?(?=^### |^## |\Z)",
        r"(?ms)^### For create_skill:\n.*?(?=^### |^## |\Z)",
        r"(?ms)^\*\*For create_skill:\*\*\n.*?(?=^### |^## |\Z)",
        r"(?ms)^## Skill Writing Principles \(for create_skill\)\n.*?(?=^## |\Z)",
        r"(?ms)^## Step 6: Maintain Skill History\n.*?(?=^## |\Z)",
        r"(?ms)^Record the validation in the paired history evidence file,\n.*?(?=^## Step 6:|\Z)",
    ):
        guide = re.sub(pattern, "", guide)
    replacements = {
        "3. **Decide** what actions to take for each skill or pattern.":
            "3. **Decide** whether evidence supports refining the allowed existing skill or skipping.",
        "- **No-skill sessions**: sessions that referenced no skill → consider\n"
        "  whether a new skill should be created.":
            "- **No-skill sessions**: use these only as context for the allowed existing skill.",
        "Before you consider any new or changed skill complete":
            "Before you consider a changed skill complete",
        "For every skill you create or modify:":
            "For the allowed existing skill when you modify it:",
        "Edit the existing `skills/<name>/` bundle in place. `SKILL.md` remains the\n"
        "entrypoint, but you may also update supporting files such as\n"
        "`references/`, `scripts/`, `assets/`, and `history/` when the evidence\n"
        "shows the skill needs them.":
            "Edit only the existing `skills/<name>/SKILL.md` entrypoint. Leave every other "
            "file and directory unchanged.",
    }
    for original, replacement in replacements.items():
        if original not in guide:
            raise EvolutionError("Native evolution guide changed before target-only prompt adaptation")
        guide = guide.replace(original, replacement)
    guide = "\n".join(
        line for line in guide.splitlines()
        if not re.search(r"create_skill|\bcreat(?:e|ed|es|ing)\b|new skills?", line, re.IGNORECASE)
        and "ALWAYS save the old version and evidence" not in line
        and "ALWAYS use version-based history filenames" not in line
        and "`v<N>_evidence.md`); never use date-based filenames" not in line
    ) + "\n"
    if re.search(r"create_skill|\bcreat(?:e|ed|es|ing)\b|new skills?", guide, re.IGNORECASE):
        raise EvolutionError("Target-only native guide still contains a skill-creation path")
    return guide


def _prompt(
    group: str,
    source_task_ids: list[str],
    reserved_environment_skill_names: list[str] | None = None,
    *,
    target_only: bool = False,
    controller_ab: bool = True,
) -> str:
    native = Path(__file__).resolve().parents[2] / "evolve_server/engines/EVOLVE_AGENTS.md"
    base = native.read_text()
    if target_only:
        base = _target_only_native_guide(base)
    action_scope = (
        "refine the allowed existing skill, OR skip. Do not create a new skill. "
        if target_only
        else "refine the allowed existing skill, OR create one distinct new skill, OR skip. "
    )
    creation_scope = "" if target_only else (
        "For create, choose a name absent from both the current shared bank and the reserved environment names; "
        "an existing allowed shared-bank target may still be refined even if its name is reserved. "
        "For the no-skill group, only create one new skill or skip. "
    )
    rationale_action = "refine" if target_only else "refine|create"
    exact_bundle_scope = (
        "For this target-only protocol, edit only the allowed existing skill's SKILL.md. "
        "Do not add, delete, rename, or modify any supporting or history file. "
        if target_only else ""
    )
    controller_step = (
        "The controller, separately, will run the real toolchain A/B test.\n"
        if controller_ab
        else "The controller will run structural loader preflight and directly publish the exact proposal "
        "without a task A/B rerun.\n"
    )
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
                "new_skill_creation_allowed": not target_only,
                "reserved_environment_skill_names_for_new_skills": sorted(reserved_environment_skill_names or []),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n\n"
        + (
            "This is a private proposal run, not publication or task A/B validation.\n"
            "All other existing skill bundles are read-only context. Make at most ONE proposal: "
            + action_scope
            +
            "Do not rename/delete existing skills or remove their supporting files. "
            + exact_bundle_scope
            + creation_scope
            +
            "Preserve successful procedures while correcting failures supported by these sessions. "
            "The aggregate.outcome fields are actual daytime results; do not invent scores. "
            "If making a proposal, write proposal-rationale.json in the workspace root with "
            '{"name": "skill-name", "action": "'
            + rationale_action
            + '", "reason": "evidence-based explanation", '
            '"source_task_ids": ["explicit task IDs from the supplied evidence"]}. '
            + controller_step
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


def _native_prompt() -> str:
    return (Path(__file__).resolve().parents[2] / "evolve_server/engines/EVOLVE_AGENTS.md").read_text()


def _mentioned(text: str, identifier: str) -> bool:
    return bool(re.search(r"(?<![A-Za-z0-9_./-])" + re.escape(identifier) + r"(?![A-Za-z0-9_./-])", text))


def _history_sources(bundles: list[dict], bank: dict, sessions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Resolve only explicit references in this edit's native history evidence."""
    aliases = {}
    for session in sessions:
        for alias in (session["session_id"], session["source_session_id"]):
            if alias in aliases and aliases[alias] is not session:
                raise EvolutionError("Ambiguous native session identity")
            aliases[alias] = session
    task_ids = {s["task_id"] for s in sessions}
    selected, evidence = {}, []
    for bundle in bundles:
        before = bank.get(bundle["name"], {})
        found = False
        for path in sorted(Path(bundle["bundle_dir"]).glob("history/*_evidence.md")):
            relative = path.relative_to(Path(bundle["bundle_dir"])).as_posix()
            raw = path.read_bytes()
            if before.get(relative) == raw:
                continue
            text = raw.decode()
            mentioned_tasks = {task for task in task_ids if _mentioned(text, task)}
            mentioned_sessions = {alias for alias in aliases if _mentioned(text, alias)}
            unknown_tasks = set(re.findall(r"\b\d{2}_[A-Za-z0-9_]+_task_\d+_[A-Za-z0-9_]+\b", text)) - task_ids
            session_pattern = r"(?<![A-Za-z0-9_-])[0-9a-f]{24}(?:-[0-9a-f]{12})?(?![A-Za-z0-9_-])"
            unknown_sessions = set(re.findall(session_pattern, text)) - set(aliases)
            if unknown_tasks or unknown_sessions:
                raise ValueError(f"unverifiable_sources: {bundle['name']} references non-day identities")
            resolved = {aliases[alias]["source_session_id"]: aliases[alias] for alias in mentioned_sessions}
            resolved.update({s["source_session_id"]: s for s in sessions if s["task_id"] in mentioned_tasks})
            if not resolved:
                continue
            found = True
            selected.update(resolved)
            evidence.append({"bundle": bundle["name"], "path": relative,
                             "sha256": hashlib.sha256(raw).hexdigest(),
                             "source_task_ids": sorted({s["task_id"] for s in resolved.values()}),
                             "source_session_ids": sorted(resolved)})
        if not found:
            raise ValueError(f"unverifiable_sources: {bundle['name']} has no changed native history with real day IDs")
    return [selected[key] for key in sorted(selected)], evidence


def _configured_history_sources(config: dict, bundles: list[dict], bank: dict,
                                sessions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Select the explicitly frozen source protocol without changing evidence."""
    if config.get("alignment", {}).get("source_evidence_revision") == "native-history-frozen-alias-union-v3":
        from source_evidence_v2 import resolve_history_sources_for_config
        return resolve_history_sources_for_config(config, bundles, bank, sessions)
    return _history_sources(bundles, bank, sessions)


def _origin_skill_root(origin: dict) -> Path:
    if origin.get("skill_root"):
        return Path(origin["skill_root"]).resolve()
    if origin.get("checkpoint"):
        return Path(origin["checkpoint"]).resolve().parent / "skills"
    raise EvolutionError("Completed native origin has no proven skill bytes")


async def _whole_night(attempt: Path, bank_dir: Path, bank: dict, sessions: list[dict],
                       config: dict, record: dict, summary_path: Path) -> list[dict]:
    """One complete native session; the versioned collector selects its edit unit."""
    from scripts.wildclawbench.protocol import alignment_candidate_id

    prompt = attempt / "EVOLVE_AGENTS.md"
    prompt.write_text(_native_prompt())
    native_config = EvolveServerConfig(
        engine="agent", storage_backend="local", local_root=str(attempt / "private-store"),
        group_id="proposal", llm_base_url=config["llm_base_url"], llm_model=config["llm_model"],
        llm_api_key=str(config.get("local_api_key", config.get("llm_api_key", "local-experiment"))),
        llm_max_tokens=config["llm_max_tokens"], llm_temperature=config["llm_temperature"],
        openclaw_home=str(attempt / "openclaw-home"), workspace_root=str(attempt / "workspace"),
        agents_md_path=str(prompt), fresh=True, agent_timeout=config["agent_timeout"],
        history_path=str(attempt / "native-history.jsonl"),
    )
    engine = _CachedNativeEvolver(native_config)
    for name, files in bank.items():
        engine._upload_skill(parse_skill_content(name, files["SKILL.md"].decode()), files, "seed")
    for session in sessions:
        engine._bucket.put_object(f"proposal/sessions/{session['session_id']}.json",
                                  json.dumps(session, ensure_ascii=False).encode())
    engine._runner = _RecordingRunner(_runner_delegate(config),
        {**config, "_proposal_source_bank": str(bank_dir), "_night_day_sessions": sessions},
        attempt, Path(native_config.openclaw_home))
    result = await engine.run_once()
    _write_json(attempt / "native-result.json", result)
    return _collect_atomic_candidate(attempt, bank_dir, bank, sessions, config, record)


def _collect_atomic_candidate(attempt: Path, bank_dir: Path, bank: dict, sessions: list[dict],
                              config: dict, record: dict) -> list[dict]:
    from scripts.wildclawbench.protocol import alignment_candidate_id

    workspace = attempt / "workspace"
    if config.get("alignment", {}).get("native_candidate_scope") == "per-skill-v1":
        if config.get("native_night_flow"):
            from scripts.injection.native_night_flow import completed_per_skill_validation
            record["native_origin"] = completed_per_skill_validation(attempt, bank_dir, config)
        skills = _origin_skill_root(record["native_origin"]) if record.get("native_origin") else workspace / "skills"
        return _collect_per_skill_candidates(attempt, bank_dir, bank, sessions, config, record,
                                             workspace=workspace, skills_root=skills)
    if config.get("native_night_flow"):
        from scripts.injection.native_night_flow import rejected_atomic_proposal
        rejection = rejected_atomic_proposal(attempt, bank_dir, config)
        if rejection is not None:
            record["outcome"] = "native_validation_failed"
            record["quarantines"] = [rejection]
            _write_json(attempt / "quarantined-changeset.json", rejection)
            return []
    _tree_hash(workspace / "skills")
    final = {p.name: read_skill_bundle(p) for p in sorted((workspace / "skills").iterdir())
             if p.is_dir() and (p / "SKILL.md").is_file()}
    deleted = sorted(set(bank) - set(final))
    if deleted:
        # Native collect_changes ignores whole-skill deletions. Keep their
        # parent bytes in the effective bank without rewriting raw workspace evidence.
        record["ignored_skill_deletions"] = [{
            "name": name, "retained_parent_bundle_sha256": _tree_hash(bank_dir / name)
        } for name in deleted]
        final.update({name: bank[name] for name in deleted})
    changed = [name for name in final if final[name] != bank.get(name)]
    if not any(not c["path"].startswith("history/") for name in changed
               for c in _changed_files(bank.get(name, {}), final[name])):
        record["outcome"] = "native_skip"
        return []
    bundles = []
    for name in changed:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise EvolutionError("Unsafe native skill name")
        before, after = bank.get(name, {}), final[name]
        exported = attempt / "candidate" / name
        write_skill_bundle(exported, after, clean=True)
        for source in (workspace / "skills" / name).rglob("*"):
            if source.is_file():
                (exported / source.relative_to(workspace / "skills" / name)).chmod(source.stat().st_mode & 0o777)
        bundles.append({"name": name, "action": "refine" if name in bank else "create",
            "bundle_dir": str(exported), "bundle_sha256": _tree_hash(exported),
            "native_tree_sha256": bundle_tree_sha256(after),
            "base_bundle_sha256": _tree_hash(bank_dir / name) if name in bank else None,
            "changed_files": _changed_files(before, after), "files": bundle_file_records(after)})
    candidate = {"name": "native-night-changeset", "action": "atomic_changeset", "bundles": bundles,
                 "source_bank_sha256": record["source_bank_sha256"], "engine": "native-agent-whole-night",
                 "validation_status": "not_validated", "proposal_artifact": str(attempt / "candidate.json")}
    try:
        selected, evidence = _history_sources(bundles, bank, sessions)
    except ValueError as error:
        candidate.update(source_task_ids=[], source_session_ids=[], source_evidence=[], reason=str(error))
        _attach_typed_sources(candidate, config)
        candidate["candidate_id"] = alignment_candidate_id(candidate, config)
        candidate["reason"] = "unverifiable_sources"
        record["quarantines"] = [{**candidate, "source_error": str(error)}]
        _write_json(attempt / "quarantined-changeset.json", record["quarantines"][0])
        record["outcome"] = "unverifiable_sources"
        return []
    candidate.update(source_task_ids=sorted({s["task_id"] for s in selected}),
                     source_session_ids=sorted(s["source_session_id"] for s in selected), source_evidence=evidence,
                     source_outcomes=[{"task_id": s["task_id"], "session_id": s["source_session_id"],
                                       "outcome": s.get("outcome", {})} for s in selected])
    _attach_typed_sources(candidate, config)
    candidate["candidate_id"] = alignment_candidate_id(candidate, config)
    _write_json(attempt / "candidate.json", candidate)
    return [candidate]



def _per_skill_dependencies(name: str, files: dict, bank: dict, final: dict) -> dict:
    """Bind explicit cross-skill resource files to unchanged parent bytes.

    Directory/dynamic references and resources needing another proposed edit
    cannot be validated as independent skill replacements and are quarantined.
    History is evidence, not an executable dependency declaration.
    """
    # Relative traversals start at a path token, never inside an API-route ellipsis.
    pattern = re.compile(r"(?:~?/root/|~/)?skills/[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[^\s`\"'<>()[\]{}]*)?|(?<![\w./-])(?:\.\./)+[^\s`\"'<>()[\]{}]+")
    dependencies, references = {}, []
    for filename, raw in sorted(files.items()):
        if filename.startswith("history/"):
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if re.search(r"skills/[${<]", text):
            raise EvolutionError(f"unresolved_cross_skill_dependency: {filename} has a dynamic skill path")
        for match in pattern.finditer(text):
            value = match.group().rstrip(".,;:")
            token_start = max(text.rfind(" ", 0, match.start()), text.rfind("\n", 0, match.start())) + 1
            if "://" in text[token_start:match.start()]:
                continue  # A remote URL path is not a workspace resource.
            value = value.split("#", 1)[0].split("?", 1)[0]
            if "skills/" in value and not value.startswith("../"):
                resolved = posixpath.normpath(value[value.index("skills/"):])
            else:
                resolved = posixpath.normpath(posixpath.join("skills", name, posixpath.dirname(filename), value))
            parts = PurePosixPath(resolved).parts
            if len(parts) < 2 or parts[0] != "skills":
                raise EvolutionError(f"unresolved_cross_skill_dependency: {filename}: {value}")
            target = parts[1]
            if target == name:
                continue
            relative = "/".join(parts[2:])
            original = bank.get(target, {}).get(relative)
            current = final.get(target, {}).get(relative)
            if original is None or current != original:
                raise EvolutionError(f"unresolved_cross_skill_dependency: {filename} references new, changed or missing {target}/{relative}")
            dependencies[(target, relative)] = {"skill": target, "path": relative,
                "sha256": hashlib.sha256(original).hexdigest()}
            references.append({"source_file": filename, "reference": value, "skill": target, "path": relative})
    return {"policy": "parent-bound-cross-skill-files-v1",
            "scan": "explicit-static-workspace-and-relative-paths",
            "dependencies": [dependencies[key] for key in sorted(dependencies)], "references": references}


def _collect_per_skill_candidates(attempt: Path, bank_dir: Path, bank: dict, sessions: list[dict],
                                  config: dict, record: dict, *, workspace: Path,
                                  skills_root: Path | None = None) -> list[dict]:
    """Export independent procedural edits from one completed whole-night run."""
    from scripts.wildclawbench.protocol import alignment_candidate_id

    if config.get("alignment", {}).get("native_candidate_scope") != "per-skill-v1":
        raise EvolutionError("Independent collection requires its explicit per-skill protocol")
    parent_hash = _tree_hash(bank_dir)
    if parent_hash != record["source_bank_sha256"]:
        raise EvolutionError("Per-skill collection parent bank changed")
    skills_root = skills_root if skills_root is not None else workspace / "skills"
    final, invalid = {}, {}
    for path in sorted(skills_root.iterdir()):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", path.name):
            invalid[path.name] = "unsafe_skill_name"
            continue
        if not path.is_dir() or path.is_symlink():
            invalid[path.name] = "invalid_skill_directory"
            continue
        try:
            _tree_hash(path)
            bundle = read_skill_bundle(path)
            if "SKILL.md" not in bundle:
                raise EvolutionError("missing_skill_entrypoint")
            final[path.name] = bundle
        except (EvolutionError, OSError, ValueError) as error:
            invalid[path.name] = str(error)
    candidates, quarantines, history_only = [], [], []

    def quarantine(name, reason, *, candidate=None, detail=None):
        value = candidate or {"name": name, "action": "atomic_changeset", "bundles": [],
            "source_bank_sha256": parent_hash, "source_task_ids": [], "source_session_ids": [], "source_evidence": []}
        value = {**value, "reason": reason, "validation_status": "quarantined", "source_error": detail}
        value.setdefault("candidate_id", _hash({"protocol": config["protocol_hash"], "skill": name,
            "parent": parent_hash, "bundles": value["bundles"], "source_evidence": value["source_evidence"],
            "reason": reason, "detail": detail})[:24])
        quarantines.append(value)
        _write_json(attempt / "quarantined-skills" / (_safe_id(name) + ".json"), value)

    for name in sorted(set(bank) | set(final) | set(invalid)):
        if name in invalid or name not in final:
            quarantine(name, "invalid_skill_bundle" if name in invalid else "unsupported_skill_deletion",
                       detail=invalid.get(name, "Parent skill was deleted; its published bytes are retained"))
            continue
        before, after = bank.get(name, {}), final[name]
        changes = _changed_files(before, after)
        if not changes:
            continue
        if not any(not item["path"].startswith("history/") for item in changes):
            history_only.append(name)
            continue
        exported = attempt / "candidate" / name
        write_skill_bundle(exported, after, clean=True)
        for source in (skills_root / name).rglob("*"):
            if source.is_file():
                (exported / source.relative_to(skills_root / name)).chmod(source.stat().st_mode & 0o777)
        bundle = {"name": name, "action": "refine" if name in bank else "create", "bundle_dir": str(exported),
            "bundle_sha256": _tree_hash(exported), "native_tree_sha256": bundle_tree_sha256(after),
            "base_bundle_sha256": _tree_hash(bank_dir / name) if name in bank else None,
            "changed_files": changes, "files": bundle_file_records(after)}
        candidate = {"name": name, "action": "atomic_changeset", "bundles": [bundle],
            "source_bank_sha256": parent_hash, "engine": "native-agent-whole-night-per-skill",
            "candidate_scope": "per-skill-v1", "validation_status": "not_validated",
            "proposal_artifact": str(attempt / "skill-candidates" / (name + ".json")),
            "source_task_ids": [], "source_session_ids": [], "source_evidence": []}
        try:
            candidate["dependency_contract"] = _per_skill_dependencies(name, after, bank, final)
            selected, evidence = _configured_history_sources(config, [bundle], bank, sessions)
        except (ValueError, EvolutionError) as error:
            quarantine(name, "unresolved_cross_skill_dependency" if "cross_skill_dependency" in str(error)
                       else "unverifiable_sources", candidate=candidate, detail=str(error))
            continue
        candidate.update(source_task_ids=sorted({s["task_id"] for s in selected}),
            source_session_ids=sorted(s["source_session_id"] for s in selected), source_evidence=evidence,
            source_outcomes=[{"task_id": s["task_id"], "session_id": s["source_session_id"],
                              "outcome": s.get("outcome", {})} for s in selected])
        _attach_typed_sources(candidate, config)
        candidate["candidate_id"] = alignment_candidate_id(candidate, config)
        _write_json(Path(candidate["proposal_artifact"]), candidate)
        candidates.append(candidate)
    if _tree_hash(bank_dir) != parent_hash:
        raise EvolutionError("Per-skill collection modified the immutable parent")
    record.update(outcome="per_skill_candidates" if candidates else "per_skill_skip_or_quarantine",
        candidates=candidates, quarantines=quarantines, ignored_history_only_skills=history_only,
        candidate_scope="per-skill-v1")
    _write_json(attempt / "per-skill-collection.json", {**record, "collection_status": "completed"})
    return candidates


def collect_completed_native_proposal(source_attempt: Path, bank_dir: Path, sessions: list[dict],
                                     config: dict, target_dir: Path) -> list[dict]:
    """Recollect a proven native0 snapshot or legacy receipt without model calls."""
    from scripts.injection.native_night_flow import completed_per_skill_validation

    source_attempt, bank_dir, target_dir = map(lambda p: Path(p).resolve(), (source_attempt, bank_dir, target_dir))
    if (target_dir.exists() or target_dir.is_relative_to(source_attempt) or target_dir.is_relative_to(bank_dir)
            or config.get("alignment", {}).get("native_candidate_scope") != "per-skill-v1"
            or not (config.get("native_checkpoint_origin") or config.get("native_legacy_origin"))):
        raise EvolutionError("Recollection requires an isolated target and explicit completed-native evidence")
    if config.get("native_checkpoint_origin") and config.get("native_legacy_origin"):
        raise EvolutionError("Recollection cannot select two different native origins")
    origin = completed_per_skill_validation(source_attempt, bank_dir, config)
    bank = {p.name: read_skill_bundle(p) for p in bank_dir.iterdir()
            if p.is_dir() and (p / "SKILL.md").is_file()}
    normalized = [_normalize_session(s, set(bank), restrict_to_bank=False) for s in sessions]
    archived = [_normalize_session(json.loads(p.read_text()), set(bank), restrict_to_bank=False)
                for p in sorted((source_attempt / "raw-sessions").glob("*.json"))]
    if (not normalized or len({s["source_session_id"] for s in normalized}) != len(normalized)
            or sorted(normalized, key=lambda s: s["source_session_id"]) != sorted(archived, key=lambda s: s["source_session_id"])):
        raise EvolutionError("Recollection Day identities, trajectories or scores differ from the original native input")
    target_dir.mkdir(parents=True, exist_ok=False)
    record = {"status": "completed", "source_bank_sha256": _tree_hash(bank_dir),
        "source_attempt": str(source_attempt), "attempt_dir": str(target_dir), "native_origin": origin,
        "origin_protocol_hash": origin["source_protocol_hash"], "protocol_hash": config["protocol_hash"],
        "new_native_calls": 0, "new_summary_calls": 0}
    _write_json(target_dir / "native-origin.json", record)
    # Both origin types prove immutable original bytes outside a moving Agent
    # workspace. Legacy receipts never impersonate a new native checkpoint.
    candidates = _collect_per_skill_candidates(target_dir, bank_dir, bank, normalized, config, record,
        workspace=source_attempt / "workspace", skills_root=_origin_skill_root(origin))
    if completed_per_skill_validation(source_attempt, bank_dir, config) != origin:
        raise EvolutionError("Original native checkpoint changed during read-only recollection")
    return candidates


def resume_atomic_proposal(attempt_dir: Path, bank_dir: Path, sessions: list[dict], config: dict,
                           message: str) -> list[dict]:
    """Resume a cloned incomplete native session, not a fresh run_once invocation."""
    from scripts.injection.native_night_flow import continue_native

    attempt, bank_dir = Path(attempt_dir).resolve(), Path(bank_dir).resolve()
    if (attempt / "candidate.json").exists() or (attempt.parent / "proposal-summary.json").exists():
        raise EvolutionError("Continuation was already collected; inspect its immutable result")
    bank = {p.name: read_skill_bundle(p) for p in bank_dir.iterdir()
            if p.is_dir() and (p / "SKILL.md").is_file()}
    normalized = [_normalize_session(s, set(bank), restrict_to_bank=False) for s in sessions]
    prepared = json.loads((attempt / "continuation.json").read_text())
    if _tree_hash(bank_dir) != prepared["parent_bank_sha256"]:
        raise EvolutionError("Continuation bank differs from its original Day evidence")
    if len({s["source_session_id"] for s in normalized}) != len(normalized):
        raise EvolutionError("Continuation has duplicate real Day identities")
    original_sessions = [json.loads(p.read_text()) for p in sorted((attempt / "raw-sessions").glob("*.json"))]
    expected_sessions = sorted(normalized, key=lambda s: s["source_session_id"])
    supplied_sessions = sorted([_normalize_session(s, set(bank), restrict_to_bank=False) for s in original_sessions],
                               key=lambda s: s["source_session_id"])
    if expected_sessions != supplied_sessions:
        raise EvolutionError("Continuation Day evidence differs from original native input")
    result = continue_native(_runner_delegate(config), attempt, bank_dir,
                             {**config, "_night_day_sessions": normalized}, message)
    record = {"status": "running", "source_bank_sha256": _tree_hash(bank_dir),
              "attempt_dir": str(attempt), "native_session_continuation": prepared,
              "candidates": [], "new_fresh_evolution_calls": 0}
    if result.returncode:
        raise EvolutionError(f"Native continuation needs recovery, returncode {result.returncode}; see {attempt}")
    record["candidates"] = _collect_atomic_candidate(attempt, bank_dir, bank, normalized, config, record)
    record["status"] = "completed"
    _write_json(attempt.parent / "proposal-summary.json", record)
    return record["candidates"]


async def _propose(round_dir: Path, bank_dir: Path, sessions: list[dict], config: dict) -> list[dict]:
    from scripts.wildclawbench.protocol import alignment_settings

    alignment = alignment_settings(config)
    if alignment and not config.get("protocol_hash"):
        raise EvolutionError("Whole-night proposals require the frozen protocol identity")
    started = time.monotonic()
    bank_hash = _tree_hash(bank_dir)
    bank = {
        path.name: read_skill_bundle(path)
        for path in sorted(bank_dir.iterdir())
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    if not bank:
        raise EvolutionError("The deployed bank has no skill bundles")
    normalized = [_normalize_session(session, set(bank), restrict_to_bank=not alignment) for session in sessions]
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
    if config.get("target_evolution_scope") is not None:
        settings["target_evolution_scope"] = config["target_evolution_scope"]
    if alignment:
        settings["alignment"] = alignment
    fingerprint = _hash(
        {
            "bank_hash": bank_hash,
            "sessions": normalized,
            "settings": settings,
            "native_prompt_sha256": hashlib.sha256(
                (_native_prompt() if alignment else _prompt(
                    "__no_skill__", [], config.get("environment_skill_names"),
                    target_only=config.get("target_evolution_scope") is not None,
                    controller_ab=config.get("night_protocol", {}).get("candidate_task_ab_validation", True),
                )).encode()
            ).hexdigest(),
        }
    )
    output = round_dir / "evolution"
    summary_path = output / "proposal-summary.json"
    saved = None
    if summary_path.is_file():
        saved = json.loads(summary_path.read_text())
        if saved.get("status") == "completed" and saved.get("input_fingerprint") == fingerprint:
            for candidate in saved["candidates"] + saved.get("quarantines", []):
                for bundle in candidate.get("bundles", [candidate]):
                    if _tree_hash(Path(bundle["bundle_dir"])) != bundle["bundle_sha256"]:
                        raise EvolutionError("Cached exported candidate bytes changed")
            return saved["candidates"]
    resilient = None
    if config.get("execution_protocol"):
        from scripts.injection import resilient_runner
        resilient = resilient_runner.execution_protocol(config)
    if (resilient and saved and saved.get("status") == "running"
            and saved.get("phase") == "summarizing"
            and saved.get("input_fingerprint") == fingerprint):
        attempt = Path(saved["attempt_dir"]).resolve()
        if (not attempt.is_dir() or attempt.is_symlink()
                or not attempt.is_relative_to(output.resolve())):
            raise EvolutionError("Summary checkpoint attempt directory is missing or outside its Night")
        record = saved
    else:
        if (resilient and saved and saved.get("status") == "running"
                and saved.get("input_fingerprint") == fingerprint):
            raise EvolutionError(
                "A native evolution call already started; resume validation from its immutable checkpoint")
        attempt = output / ("attempt-" + uuid.uuid4().hex[:12])
        attempt.mkdir(parents=True)
        record = {
            "status": "running",
            "phase": "summarizing",
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
            from scripts.injection import resilient_runner
            budget = resilient_runner.active_budget(config) if resilient else None
            request_timeout = min(600.0, budget.require_remaining("native-summary-start")) if budget else 600.0
            client = AsyncLLMClient(
                api_key=str(config.get("local_api_key", config.get("llm_api_key", "local-experiment"))),
                base_url=config["llm_base_url"],
                model=config["llm_model"],
                max_tokens=config["llm_max_tokens"],
                temperature=config["llm_temperature"],
                max_retries=1 if resilient else 5,
                request_timeout_seconds=request_timeout,
                retry_transient_only=bool(resilient),
            )
            summarizer = _BoundedSummarizer(client, config)
            if resilient:
                pending = []
                for session in normalized:
                    cache = attempt / "summary-checkpoints" / (_safe_id(session["session_id"]) + ".json")
                    input_sha256 = _hash(session)
                    saved_summary = json.loads(cache.read_text()) if cache.is_file() else None
                    if saved_summary:
                        if saved_summary.get("input_sha256") != input_sha256:
                            raise EvolutionError("Summary checkpoint belongs to different session evidence")
                        session.update(saved_summary["normalized_session"])
                        continue

                    async def summarize_one(row=session, path=cache, source_hash=input_sha256):
                        _extract_session_metadata(row)
                        row["_trajectory"] = build_session_trajectory(row)
                        row["_summary"] = await summarize_session(summarizer, row)
                        if row["_summary"]:
                            _write_json(path, {
                                "input_sha256": source_hash,
                                "normalized_session": _summary_checkpoint_session(row),
                            })

                    pending.append(summarize_one())
                if pending:
                    await asyncio.gather(*pending)
                if summarizer.circuit_error is not None:
                    raise summarizer.circuit_error
            else:
                await summarize_sessions_parallel(summarizer, normalized)
            if any(not session.get("_summary") for session in normalized):
                raise EvolutionError("A native session summary failed; refusing incomplete evidence")
        record["summarization_elapsed_seconds"] = time.monotonic() - summary_started
        # Avoid native aggregation's set-valued metadata in persisted JSON.
        for session in normalized:
            if not alignment:
                _restrict_bank_references(session, set(bank))
            session["_skills_referenced"] = sorted(session["_skills_referenced"])
        target_scope = _apply_target_evolution_scope(normalized, set(bank), config)
        if target_scope is not None:
            record["target_evolution_scope"] = target_scope
        for session in normalized:
            _write_json(attempt / "native-summaries" / f"{session['session_id']}.json", session)
        record["phase"] = "native_running"
        _write_json(summary_path, record)
        groups = aggregate_sessions_by_skill(normalized)
        if alignment:
            record["groups"] = [{"evidence_group": name,
                "source_session_ids": sorted(s["source_session_id"] for s in members),
                "source_task_ids": sorted({s["task_id"] for s in members})} for name, members in sorted(groups.items())]
            if normalized:
                record["candidates"] = await _whole_night(
                    attempt, bank_dir, bank, normalized, config, record, summary_path
                )
            else:
                record["outcome"] = "native_skip"
            if _tree_hash(bank_dir) != bank_hash:
                raise EvolutionError("The deployed bank changed during native evolution")
            record.update(status="completed", elapsed_seconds=time.monotonic() - started)
            _write_json(summary_path, record)
            return record["candidates"]
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
            prompt_path.write_text(_prompt(
                scope,
                task_ids,
                config.get("environment_skill_names"),
                target_only=config.get("target_evolution_scope") is not None,
                controller_ab=config.get("night_protocol", {}).get("candidate_task_ab_validation", True),
            ))
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
                _attach_typed_sources(candidate, config)
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
