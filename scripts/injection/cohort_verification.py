"""Verify Night defense delivery in the protected branch's actual model requests."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def verify_night_safe_prompt(run_dir: Path, branch: Path, expected_prompt: str) -> dict:
    """Require exact bootstrap bytes in successful, branch-bound SYSTEM requests.

    Summaries, validation requests, other branches and trajectory text cannot
    establish delivery. This reads existing evidence and never changes artifacts.
    """
    audit = {"verified": False, "policy": "branch-system-bootstrap-v1", "evidence": []}
    try:
        run_dir, branch = Path(run_dir).resolve(), Path(branch).resolve()
        if not branch.is_relative_to(run_dir / "branches"):
            raise ValueError("Protected branch is outside this run")
        root = branch / "nights/01/proposals/evolution"
        summary = _read(root / "proposal-summary.json")
        attempt = Path(summary["attempt_dir"]).resolve()
        if not attempt.is_relative_to(root.resolve()):
            raise ValueError("Native attempt is outside the protected branch")
        workspace = attempt / "workspace"
        agents_path = workspace / "AGENTS.md"
        agents_raw = agents_path.read_bytes()
        agents = agents_raw.decode("utf-8")
        prefix = expected_prompt + "\n\n"
        if not expected_prompt or not agents.startswith(prefix):
            raise ValueError("The exact defense is not the bootstrap prefix")
        binding = _read(workspace / "night-safe-prompt.json")
        if binding.get("sha256") != _hash(expected_prompt.encode()):
            raise ValueError("Defense hash differs from the bootstrap binding")
        bound_path = binding.get("bootstrap_path", binding.get("prefixed_bootstrap", ""))
        if Path(bound_path).resolve() != agents_path.resolve():
            raise ValueError("Defense binding belongs to another bootstrap")
        for key, raw in (("original_bootstrap_sha256", agents[len(prefix):].encode()),
                         ("defended_bootstrap_sha256", agents_raw)):
            if key in binding and binding[key] != _hash(raw):
                raise ValueError(f"Bootstrap differs from its recorded {key}")
        for key in ("normal_evolution_instructions_preserved", "normal_evolution_prompt_preserved"):
            if key in binding and binding[key] is not True:
                raise ValueError("Normal evolution bootstrap was not preserved")

        settings = _read(run_dir / "config.runtime.json")
        from evolve_server.engines import agent_workspace
        source = Path(agent_workspace.__file__)
        source_raw = source.read_bytes()
        frozen = run_dir / "frozen-source.json"
        if frozen.exists():
            records = [item for item in _read(frozen)["files"]
                       if Path(item["path"]).resolve() == source.resolve()]
            if len(records) != 1 or records[0]["sha256"] != _hash(source_raw):
                raise ValueError("Native bootstrap source differs from the frozen source")
        original = next(ast.literal_eval(node.value) for node in ast.parse(source_raw).body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "_EVOLVE_AGENTS_MD"
                                for target in node.targets))
        if agents != prefix + original:
            raise ValueError("The exact native bootstrap was not preserved after the defense")

        config = _read(attempt / "openclaw-home/.openclaw/openclaw.json")
        if Path(config["agents"]["defaults"]["workspace"]).resolve() != workspace.resolve():
            raise ValueError("OpenClaw workspace differs from the protected native attempt")
        provider = config["models"]["providers"]["sharedskill"]
        route = re.fullmatch(r"/runs/([A-Za-z0-9_.-]{1,160})/v1/?", urlsplit(provider["baseUrl"]).path)
        if not route or route[1] in {".", ".."}:
            raise ValueError("Native provider lacks an explicit Night request route")
        run_id = route[1]
        expected_model = settings.get("model_id")
        protocol = settings.get("protocol_hash")
        block = "## " + str(agents_path) + "\n" + agents
        audit.update(attempt_dir=str(attempt), run_id=run_id,
                     bootstrap_path=str(agents_path), bootstrap_sha256=_hash(agents_raw),
                     prompt_sha256=_hash(expected_prompt.encode()), protocol_hash=protocol)
        successes = 0
        evidence_root = Path(settings.get("gateway_log_dir", run_dir / "runtime/api-evidence"))
        for request in sorted((evidence_root / run_id).glob("*/request.json")):
            body = _read(request)
            if not isinstance(body.get("tools"), list) or not body["tools"]:
                continue  # Native session summaries are separate from the acting Agent.
            record = _read(request.parent / "record.json")
            if (record.get("request_id") != request.parent.name or record.get("run_id") != run_id
                    or record.get("endpoint") != "chat/completions" or record.get("role") != "main"
                    or (protocol and record.get("protocol_hash") != protocol)
                    or (expected_model and (body.get("model") != expected_model
                                            or record.get("actual_model") != expected_model))):
                raise ValueError("Captured Agent request has inconsistent route/model/protocol binding")
            systems = [message["content"] for message in body.get("messages", [])
                       if message.get("role") == "system" and isinstance(message.get("content"), str)]
            matched = [content for content in systems if block in content]
            if not matched:
                raise ValueError("An acting Agent request lacks the exact protected bootstrap in SYSTEM")
            completed = (record.get("status") == "completed" and record.get("http_status") == 200
                         and record.get("upstream_attempted") is True)
            successes += int(completed)
            audit["evidence"].append({
                "kind": "branch_bound_system_request", "request": str(request),
                "record": str(request.parent / "record.json"), "role": "system",
                "request_sha256": _hash(request.read_bytes()),
                "system_sha256": [_hash(content.encode()) for content in matched],
                "completed_http_200": completed,
            })
        if not successes:
            raise ValueError("No completed HTTP 200 acting Agent request proves defense delivery")
        audit.update(verified=True, completed_request_count=successes)
    except (OSError, ValueError, KeyError, TypeError, StopIteration, SyntaxError) as error:
        audit["error"] = f"{type(error).__name__}: {error}"
    return audit
