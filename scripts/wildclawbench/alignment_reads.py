"""Conservative read evidence from original OpenClaw tool calls and results."""

from __future__ import annotations

import hashlib
import json
import posixpath
import shlex
from pathlib import Path

READ_TOOLS = frozenset({"read", "read_file", "readFile", "file_read", "readfile"})


def _paths(call: dict) -> list[str]:
    args = call.get("arguments", {})
    if not isinstance(args, dict):
        return []
    if call.get("name") in READ_TOOLS:
        return [str(args[key]) for key in ("path", "file_path", "filePath", "filename") if args.get(key)]
    if call.get("name") not in {"exec", "bash"}:
        return []
    # Only a simple read command proves an attempt. Shell scripts, echo strings,
    # redirections and pipelines are retained in the raw transcript, not guessed.
    command = args.get("command", "")
    if not isinstance(command, str):
        return []
    try:
        words = shlex.split(command, comments=True)
    except ValueError:
        return []
    if not words or Path(words[0]).name not in {"cat", "head", "sed", "less"}:
        return []
    if any(character in command for character in (";", "|", "&", ">", "<", "`", "$", "\n")):
        return []
    return [word for word in words[1:] if word.endswith("/SKILL.md")]


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


def session(output: Path, spec: dict) -> dict:
    binding = json.loads((output / "execution-binding.json").read_text())
    bank = Path(binding["bank_dir"])
    shared = {skill["directory"]: skill for skill in binding["skills"]}
    entries, calls, attempts, successes, full_reads = [], {}, [], [], []
    tool_errors = 0
    chat = output / "chat.jsonl"
    for line in chat.read_text().splitlines() if chat.exists() else []:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        entries.append(entry)
        message = entry.get("message", {})
        content = message.get("content", [])
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict) or part.get("type") not in {"toolCall", "tool_use", "tool_call"}:
                continue
            call_id = str(part.get("id", ""))
            call = {"name": part.get("name"), "arguments": part.get("arguments", part.get("input", {}))}
            calls[call_id] = []
            for original in _paths(call):
                path = posixpath.normpath("/root/" + original[2:] if original.startswith("~/") else original)
                if not path.endswith("/SKILL.md"):
                    continue
                directory = Path(path).parent.name
                is_shared = directory in shared and path == f"/root/skills/{directory}/SKILL.md"
                attempt = {"path": path, "original_path": original, "name": directory,
                           "environment_or_shared": "shared" if is_shared else "environment",
                           "tool_call_id": call_id, "tool_name": call["name"], "success": False,
                           "full_content_verified": False}
                attempts.append(attempt)
                calls[call_id].append(attempt)
        if message.get("role") not in {"toolResult", "tool"}:
            continue
        details = message.get("details") or {}
        details = details if isinstance(details, dict) else {}
        code = details.get("exitCode")
        failed = bool(message.get("isError")) or str(details.get("status", "")).lower() in {
            "error", "failed", "failure", "aborted", "cancelled", "canceled", "timeout"
        } or (code is not None and not isinstance(code, bool) and str(code) != "0")
        tool_errors += bool(failed)
        call_id = str(message.get("toolCallId", message.get("tool_call_id", "")))
        returned = _text(content)
        for attempt in calls.get(call_id, []):
            attempt["success"] = not failed
            attempt["returned_text_sha256"] = hashlib.sha256(returned.encode()).hexdigest()
            if failed:
                continue
            successes.append(attempt)
            if attempt["environment_or_shared"] != "shared":
                continue
            original = (bank / attempt["name"] / "SKILL.md").read_bytes()
            expected = original.decode("utf-8")
            # Read may append an explicit continuation marker, and exec may add
            # status text. Require the entire unmodified document, never a prefix.
            if expected and expected in returned:
                attempt.update(full_content_verified=True, full_content_sha256=hashlib.sha256(original).hexdigest())
                full_reads.append(attempt)
    names = sorted({item["name"] for item in successes if item["environment_or_shared"] == "shared"})
    return {"task_id": spec["task_id"], "prompt_text": spec["prompt"],
            "task_prompt_text": spec.get("task_prompt", spec["prompt"]), "messages": entries,
            "read_skills": names, "actual_used_skill_refs": names, "actual_skill_reads": successes,
            "skill_read_attempts": attempts, "full_shared_skill_reads": full_reads,
            "catalog_sha256": binding["catalog_sha256"], "bank_sha256": binding["bank_sha256"],
            "read_evidence_policy": "successful-tool-result-and-exact-full-content-v1",
            "native_compaction_events": [entry for entry in entries if entry.get("type") == "compaction"],
            "raw_transcript_path": str(chat), "tool_errors": tool_errors}
