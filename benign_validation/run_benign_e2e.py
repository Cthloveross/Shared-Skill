#!/usr/bin/env python3
"""Run real benign tasks through the sharedskill HTTP workflow.

Usage: .venv/bin/python benign_validation/run_benign_e2e.py
GPT is reached through the local Codex bridge, not an OpenAI API key.
The bridge flattens message roles; this is a workflow acceptance test, not
an API-equivalent benchmark or a reproduction of paper accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbook_tools import TOOL_SCHEMAS, WorkbookTools  # noqa: E402

from evolve_server.core.config import EvolveServerConfig  # noqa: E402
from evolve_server.engines.workflow import EvolveServer  # noqa: E402
from sharedskill.api_server import SharedSkillAPIServer  # noqa: E402
from sharedskill.config_store import ConfigStore  # noqa: E402
from sharedskill.skill_hub import SkillHub  # noqa: E402
from sharedskill.skill_manager import SkillManager  # noqa: E402

SEED_SKILL = (
    "---\n"
    "name: xlsx\n"
    "description: Edit and validate local XLSX report workbooks, preserving existing cells and formatting. "
    "Use when the input and output are spreadsheet files.\n"
    "category: productivity\n"
    "---\n"
    "\n"
    "# Local XLSX reports\n"
    "\n"
    "1. Read this skill and inspect the input workbook before editing.\n"
    "2. Use update_workbook to make the requested cell changes and save a separate output.\n"
    "3. Preserve original sheet names, source values, and formatting. Use formulas for derived totals.\n"
    "4. Call validate_workbook on the saved output. Correct any reported errors and validate again.\n"
    "5. Report the completed output path only after validation succeeds.\n"
)


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def evolve_config(cfg, output: Path) -> EvolveServerConfig:
    """Keep this local acceptance run independent of global EVOLVE_* settings."""
    return EvolveServerConfig(
        engine="workflow",
        storage_backend="local",
        local_root=cfg.sharing_local_root,
        group_id=cfg.sharing_group_id,
        llm_api_key=cfg.llm_api_key,
        llm_base_url=cfg.llm_api_base,
        llm_model=cfg.llm_model_id,
        use_session_judge=True,
        use_skill_verifier=True,
        publish_mode="direct",
        skill_reload_mode="callback",
        proxy_reload_url=cfg.evolve_proxy_reload_url,
        history_path=str(output / "evolve-history.jsonl"),
        processed_log_path=str(output / "evolve-processed.json"),
        debug_dump_dir=str(output / "native-debug"),
    )


def serve(kind: str, config_file: Path, port: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = ConfigStore(config_file).to_sharedskill_config()
    if kind == "proxy":
        manager = SkillManager(cfg.skills_dir, retrieval_mode=cfg.retrieval_mode)
        app = SharedSkillAPIServer(cfg, skill_manager=manager)._build_app()
    else:
        app = EvolveServer(evolve_config(cfg, config_file.parent)).create_http_app()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


def wait_ready(client: httpx.Client, url: str, process: subprocess.Popen) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"service exited with code {process.returncode}: {url}")
        try:
            r = client.get(url, timeout=2)
            if r.status_code == 200:
                return r.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"service readiness timeout: {url}")


def run_task(
    client: httpx.Client, proxy: str, case: dict, tools: WorkbookTools, skill_path: Path, output: Path
) -> dict:
    sid = case["session_id"]
    messages = [
        {
            "role": "system",
            "content": (
                "You are a local spreadsheet agent. Execute the user's task using the supplied tools. "
                f"Read the skill file at {skill_path} before editing. "
                "Inspect the workbook, save a separate output, and validate it. "
                "When validation reports an error, correct the output and validate again. "
                "When done, return the saved output path."
            ),
        },
        {"role": "user", "content": case["user_prompt"]},
    ]
    calls = []
    headers = {"X-Session-Id": sid, "X-Turn-Type": "main"}
    artifact = output / "tasks" / sid
    for turn in range(1, 13):
        body = {
            "model": "sharedskill-model",
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "stream": False,
            "max_tokens": 2048,
        }
        r = client.post(proxy + "/v1/chat/completions", json=body, headers=headers)
        r.raise_for_status()
        response = r.json()
        save_json(artifact / f"turn-{turn:02d}.json", {"request": body, "response": response})
        message = response["choices"][0]["message"]
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            break
        for call in tool_calls:
            function = call["function"]
            name = function["name"]
            arguments = json.loads(function["arguments"])
            result = tools.execute(name, arguments)
            calls.append({"name": name, "arguments": arguments, "result": result})
            messages.append({"role": "tool", "name": name, "tool_call_id": call["id"], "content": json.dumps(result)})
        print(f"{sid}: turn {turn}, tools {','.join(c['function']['name'] for c in tool_calls)}", flush=True)
    else:
        raise RuntimeError(f"agent exceeded turn limit: {sid}")

    check = tools.execute("validate_workbook", {"path": case["output"]})
    if check.get("ok") is not True:
        raise RuntimeError(f"workbook acceptance failed: {sid}: {check}")
    skill_reads = [
        c["result"]["content"]
        for c in calls
        if c["name"] == "read" and c["result"].get("ok") and Path(c["arguments"]["path"]).resolve() == skill_path
    ]
    if not skill_reads:
        raise RuntimeError(f"agent did not actually read the skill: {sid}")

    messages.append({"role": "user", "content": "The task is complete. Confirm only the saved output path."})
    close_body = {
        "model": "sharedskill-model",
        "messages": messages,
        "stream": False,
        "max_tokens": 256,
        "session_done": True,
    }
    r = client.post(proxy + "/v1/chat/completions", json=close_body, headers=headers)
    r.raise_for_status()
    save_json(artifact / "close.json", {"request": close_body, "response": r.json()})
    report = {
        "session_id": sid,
        "case": case,
        "acceptance": check,
        "tool_calls": calls,
        "skill_read_sha256": hashlib.sha256(skill_reads[-1].encode("utf-8")).hexdigest(),
        "output_sha256": digest(Path(case["output"])),
        "validator_errors": sum(c["name"] == "validate_workbook" and not c["result"].get("ok") for c in calls),
    }
    save_json(artifact / "result.json", report)
    return report


def capture_uploaded(output: Path, sid: str) -> dict:
    source = output / "shared" / "benign-e2e" / "sessions" / f"{sid}.json"
    deadline = time.monotonic() + 20
    while not source.is_file() and time.monotonic() < deadline:
        time.sleep(0.1)
    if not source.is_file():
        raise RuntimeError(f"native client did not upload session: {sid}")
    session = json.loads(source.read_text())
    if not any(t.get("read_skills") for t in session["turns"]):
        raise RuntimeError(f"native session has no skill read attribution: {sid}")
    save_json(output / "uploaded-sessions" / source.name, session)
    return session


def validate_run(output: Path, model: str) -> int:
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory must be new or empty; existing evidence is preserved")
    output.mkdir(parents=True, exist_ok=True)
    skills = output / "client-skills"
    skill_path = skills / "xlsx" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(SEED_SKILL)
    (output / "initial-SKILL.md").write_text(SEED_SKILL)
    tools = WorkbookTools(output)
    cases = [tools.prepare_case(i) for i in range(3)]
    save_json(output / "cases.json", cases)
    bridge_port, proxy_port, evolve_port = free_port(), free_port(), free_port()
    bridge, proxy, evolve = [f"http://127.0.0.1:{p}" for p in (bridge_port, proxy_port, evolve_port)]
    config_file = output / "client.yaml"
    ConfigStore(config_file).save(
        {
            "llm": {
                "provider": "openai",
                "model_id": model,
                "api_base": bridge + "/v1",
                "api_key": "local-codex-bridge",
            },
            "proxy": {"host": "127.0.0.1", "port": proxy_port},
            "claw_type": "none",
            "configure_openclaw": False,
            "skills": {"enabled": True, "dir": str(skills), "retrieval_mode": "template"},
            "prm": {"enabled": False},
            "sharing": {
                "enabled": True,
                "backend": "local",
                "local_root": str(output / "shared"),
                "group_id": "benign-e2e",
                "user_alias": "benign-user",
                "session_upload_interval": 0,
                "skill_reload_mode": "callback",
            },
            "evolve": {"server_url": evolve, "proxy_reload_url": proxy},
            "validation": {"enabled": False},
            "dashboard": {"enabled": False},
            "record": {"enabled": True, "dir": str(output / "records")},
        }
    )
    cfg = ConfigStore(config_file).to_sharedskill_config()
    if Path(cfg.record_dir).resolve() != output / "records":
        raise RuntimeError("record directory was not isolated by configuration")
    hub = SkillHub.from_config(cfg)
    seed_push = hub.push_skills(str(skills))
    save_json(output / "seed-push.json", seed_push)
    before = hub._load_remote_manifest()["xlsx"]
    save_json(output / "manifest-before.json", before)

    processes = []
    logs = []
    commands = [
        (
            "bridge",
            [
                sys.executable,
                str(ROOT / "benign_validation/codex_bridge.py"),
                "--model",
                model,
                "--port",
                str(bridge_port),
                "--artifact-dir",
                str(output / "model-calls"),
            ],
        ),
        (
            "proxy",
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--serve",
                "proxy",
                "--config",
                str(config_file),
                "--port",
                str(proxy_port),
            ],
        ),
        (
            "evolve",
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--serve",
                "evolve",
                "--config",
                str(config_file),
                "--port",
                str(evolve_port),
            ],
        ),
    ]
    report = {
        "status": "running",
        "model": model,
        "model_access": "Codex CLI bridge",
        "message_roles_flattened": True,
        "input_source": "generated benign workbook fixtures",
        "source_project": "sharedskill",
        "upstream_project": "SkillClaw",
        "source_changes": "project branding and repository layout",
        "prm_enabled": False,
        "session_judge_enabled": True,
        "skill_verifier_enabled": True,
        "publish_mode": "direct",
        "storage_backend": "local",
        "tasks": [],
    }
    save_json(output / "run.json", report)
    try:
        for name, command in commands:
            log = (output / f"{name}.log").open("w")
            logs.append(log)
            env = {**os.environ, "SHAREDSKILL_CONFIG_FILE": str(config_file), "PYTHONUNBUFFERED": "1"}
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, cwd=output, env=env, start_new_session=True
            )
            processes.append(process)
        with httpx.Client(timeout=600, trust_env=False) as client:
            for base, route, process in zip(
                (bridge, proxy, evolve), ("/healthz", "/healthz", "/status"), processes, strict=True
            ):
                wait_ready(client, base + route, process)
            print(f"Native proxy and evolver ready; model={model}", flush=True)
            for case in cases[:2]:
                task = run_task(client, proxy, case, tools, skill_path, output)
                report["tasks"].append(task)
                capture_uploaded(output, case["session_id"])
                save_json(output / "run.json", report)

            r = client.post(evolve + "/trigger")
            r.raise_for_status()
            cycle = r.json()
            save_json(output / "evolution-cycle.json", cycle)
            if cycle["sessions"] != 2 or cycle["skill_groups"] != 1 or cycle["no_skill_sessions"] != 0:
                raise RuntimeError(f"unexpected native aggregation: {cycle}")
            if cycle.get("had_processing_error"):
                raise RuntimeError("native evolution reported a processing error")
            judge, verifier = cycle["session_judge"], cycle["skill_verifier"]
            if not judge["enabled"] or judge["judged_sessions"] != 2 or judge["scored_sessions"] != 2:
                raise RuntimeError("native session judge did not successfully score both sessions")
            if not verifier["enabled"] or verifier["verified_skills"] < 1 or verifier["accepted"] < 1:
                raise RuntimeError("native skill verifier did not accept an evolved skill")
            if cycle["uploaded_skills"] < 1:
                raise RuntimeError("native evolver did not publish an accepted new skill; see evolution-cycle.json")
            after = hub._load_remote_manifest()["xlsx"]
            save_json(output / "manifest-after.json", after)
            if after["version"] <= before["version"]:
                raise RuntimeError("skill registry version did not advance")
            r = client.post(proxy + "/internal/reload-skills")
            r.raise_for_status()
            save_json(output / "client-reload.json", r.json())
            remote_md = hub._bucket.get_object("benign-e2e/skills/xlsx/SKILL.md").read()
            if remote_md != skill_path.read_bytes():
                raise RuntimeError("client skill does not match the published shared skill")
            if skill_path.read_text() == SEED_SKILL:
                raise RuntimeError("published skill content was not changed")
            (output / "published-SKILL.md").write_bytes(remote_md)
            reuse = run_task(client, proxy, cases[2], tools, skill_path, output)
            report["tasks"].append(reuse)
            capture_uploaded(output, cases[2]["session_id"])
            if reuse["skill_read_sha256"] != hashlib.sha256(remote_md).hexdigest():
                raise RuntimeError("subsequent task did not read the published skill version")
            report.update(
                {
                    "status": "passed",
                    "native_cycle": cycle,
                    "version_before": before["version"],
                    "version_after": after["version"],
                    "shared_client_skill_sha256": digest(skill_path),
                    "reuse_verified": True,
                }
            )
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "output": str(output),
                        "tasks": len(report["tasks"]),
                        "version_before": before["version"],
                        "version_after": after["version"],
                    },
                    indent=2,
                ),
                flush=True,
            )
    except Exception as exc:
        report.update({"status": "failed", "error": str(exc)})
        raise
    finally:
        save_json(output / "run.json", report)
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        for log in logs:
            log.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "benign_validation/results" / ("gpt56-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")),
    )
    parser.add_argument("--serve", choices=("proxy", "evolve"), help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve, args.config, args.port)
        return 0
    return validate_run(args.output.resolve(), args.model)


if __name__ == "__main__":
    raise SystemExit(main())
