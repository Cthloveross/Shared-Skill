"""Offline task/source/input audit and model-free task-image deadline checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import environment
from scripts.wildclawbench import provenance
from scripts.wildclawbench.protocol import discover_tasks, load_config, public_config, write_json
from scripts.wildclawbench.task_runtime import _execution_preamble, task_spec


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _git(source, *args):
    # All requested blobs already exist in the pinned local checkout.
    import os
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(["git", "-C", str(source), *args], check=True,
                          capture_output=True, env=env).stdout


def _object(source, revision, relative):
    return _git(source, "show", f"{revision}:{relative}")


def offline_tasks(config):
    """Compare every selected task and initial skill file with fixed Git objects.

    Inputs reuse the existing provenance byte baseline and validate current file
    fingerprints/directory inventories. This is explicitly not another GB hash pass.
    """
    source = environment.paths(config)["upstream"]
    revision = config["benchmark_revision"]
    checkout_revision = _git(source, "rev-parse", "HEAD").decode().strip()
    tasks = discover_tasks(config)
    baseline_runtime = environment.paths(config)["runtime"]
    manifest_path = baseline_runtime / "prepared/input-files.json"
    if not manifest_path.is_file():
        baseline_runtime = Path(config.get("shared_runtime_dir", baseline_runtime))
        manifest_path = baseline_runtime / "prepared/input-files.json"
    manifest = json.loads(manifest_path.read_text())
    input_config = {**config, "runtime_dir": str(baseline_runtime)}
    rows, failures = [], []
    for task in tasks:
        relative = Path(task["source_path"]).relative_to(source).as_posix()
        original = _object(source, revision, relative)
        actual = Path(task["source_path"]).read_bytes()
        # The upstream parser calls load_dotenv at import time; disable only that
        # unrelated side effect, retaining its actual parsing implementation.
        with patch("dotenv.load_dotenv", return_value=False):
            spec = task_spec(task["task_id"], {**config, "task": task})
        input_record = manifest["tasks"].get(task["task_id"], {})
        try:
            checked = provenance.validate_task_inputs(
                input_config, {**task, "input_sha256": input_record.get("input_sha256")})
        except Exception as error:
            checked = {"validated": False, "error": f"{type(error).__name__}: {error}"}
        literals = sorted(set(re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", actual.decode())))
        sections = {key: {"sha256": _sha(str(spec.get(key, "")).encode()),
                          "characters": len(str(spec.get(key, "")))}
                    for key in ("prompt", "automated_checks", "warmup", "env", "skills")}
        row = {"task_id": task["task_id"], "short_id": task["short_id"], "user_id": task["user_id"],
               "source_path": str(Path(task["source_path"])), "upstream_path": relative,
               "upstream_source_url": f"https://github.com/InternLM/WildClawBench/blob/{revision}/{relative}",
               "expected_sha256": _sha(original), "actual_sha256": _sha(actual),
               "source_bytes_match": original == actual,
               "selected_manifest_hash_matches": task["source_sha256"] == _sha(actual),
               "timeout_seconds": spec["timeout_seconds"],
               "timeout_matches_manifest": spec["timeout_seconds"] == task["timeout_seconds"],
               "section_hashes": sections, "skills_declaration": spec.get("skills", ""),
               "workspace_path": spec["workspace_path"], "input": {**checked,
                   "bytes": input_record.get("bytes"), "workspace_exists": input_record.get("workspace_exists")},
               "execution_preamble_sha256": _sha(_execution_preamble(task["timeout_seconds"]).encode()),
               "fixture_facts": {"literal_iso_dates": literals,
                   "contains_mock_references": bool(re.search(r"\bmock\w*\b", actual.decode(), re.I)),
                   "official_bytes_preserved": True}}
        if not all((row["source_bytes_match"], row["selected_manifest_hash_matches"],
                    row["timeout_matches_manifest"], checked["validated"])):
            failures.append(task["short_id"])
        rows.append(row)
    skill_files = []
    initial = config["initial_skill_names"]
    for name in initial:
        root = source / "skills" / name
        expected_names = _git(
            source, "ls-tree", "-r", "--name-only", revision, "--", "skills/" + name).decode().splitlines()
        actual_names = sorted(path.relative_to(source).as_posix() for path in root.rglob("*") if path.is_file())
        if sorted(expected_names) != actual_names:
            failures.append("initial-skill-inventory:" + name)
        for relative in sorted(set(expected_names) | set(actual_names)):
            expected = _object(source, revision, relative) if relative in expected_names else None
            current = (source / relative).read_bytes() if relative in actual_names else None
            same = expected is not None and expected == current
            skill_files.append({"path": relative, "expected_sha256": _sha(expected) if expected is not None else None,
                                "actual_sha256": _sha(current) if current is not None else None, "matches": same})
            if not same:
                failures.append("initial-skill:" + relative)
    parser = "src/utils/task_parser.py"
    parser_match = _object(source, revision, parser) == (source / parser).read_bytes()
    if not parser_match:
        failures.append("upstream-task-parser")
    ids = [row["short_id"] for row in rows]
    if len(ids) != 30 or len(set(ids)) != 30:
        failures.append("selected-task-count")
    if checkout_revision != revision:
        failures.append("checkout-revision")
    return {"schema_version": 1, "observed_at": datetime.now(UTC).isoformat(),
            "passed": not failures, "failure_ids": failures,
            "scope": ("30 static official task definitions, initial skills and cached input provenance; "
                      "no Agent execution or model calls"),
            "benchmark_revision": revision, "checkout_revision": checkout_revision,
            "dataset_revision": config["dataset_revision"], "task_manifest_sha256": config.get("task_manifest_sha256"),
            "upstream_task_parser_bytes_match": parser_match, "tasks": rows, "initial_skill_files": skill_files,
            "input_verification": {"baseline_manifest": str(manifest_path),
                "baseline_manifest_sha256": _sha(manifest_path.read_bytes()),
                "mode": "prior-content-hashes-plus-current-stat-and-directory-validation", "bytes_rehashed": 0,
                "selected_file_count": sum(row["input"].get("file_count", 0) for row in rows),
                "selected_bytes": sum(row["input"].get("bytes", 0) for row in rows)},
            "difference_classification": {"unexpected_source_or_input_differences": failures,
                "intentional_runtime_adaptations_not_applied_by_this_audit": [
                    "execution preamble", "local model/auxiliary service routing", "external judge routing",
                    "shared catalog injection"],
                "task_fixture_limits": (
                    "Literal dates and mock references above are official source facts. Offline parsing does not test "
                    "real-time assumptions, external website availability, or mock behavioral equivalence; "
                    "official task definitions were not changed.")},
            "no_model_calls": True, "dotenv_loading_disabled": True}


_SUPERVISOR_PROBE = r'''
import json,subprocess,sys,time
from pathlib import Path
root=Path('/tmp/offline-supervisor');root.mkdir()
service=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
rows=[]
try:
 for name,timeout in [('deadline-detached',.6),('normal-detached',3),('deadline-double-fork',.6)]:
  target=root/(name+'.late');armed=root/(name+'.armed');evidence=root/(name+'.json')
  lines=['import os,time']
  if name=='deadline-double-fork':
   lines+=['if os.fork():os._exit(0)','os.setsid()','if os.fork():os._exit(0)']
  lines+=['open('+repr(str(armed))+',"w").write(str(os.getpid()))','time.sleep(1.3)',
          'open('+repr(str(target))+',"w").write("late")']
  writer=chr(10).join(lines)
  code=chr(10).join(['import subprocess,sys,time,os',
       'subprocess.Popen([sys.executable,"-c",'+repr(writer)+'],start_new_session=True)',
       'limit=time.monotonic()+.4',
       'while not os.path.exists('+repr(str(armed))+') and time.monotonic()<limit:time.sleep(.01)',
       'print("agent done")' if name=='normal-detached' else 'time.sleep(30)'])
  process=subprocess.run([sys.executable,'/tmp/agent_supervisor.py','--timeout',str(timeout),'--evidence',str(evidence),'--',sys.executable,'-c',code],capture_output=True,text=True,timeout=8)
  time.sleep(1.5)
  stopped=json.loads(evidence.read_text());expected=0 if name=='normal-detached' else 124
  armed_pid=armed.read_text() if armed.exists() else None
  armed_stopped=armed_pid in stopped['stopped_process_identities']
  passed=(process.returncode==expected and stopped['quiescent'] and not target.exists()
          and service.poll() is None and armed_stopped)
  rows.append({'case':name,'passed':passed,'returncode':process.returncode,'expected_returncode':expected,
               'writer_armed_pid':armed_pid,'armed_pid_was_stopped':armed_stopped,
               'late_file_exists_after_wait':target.exists(),'unrelated_service_alive':service.poll() is None,
               'supervisor':stopped,
               'stderr':process.stderr[-1000:]})
finally:
 service.terminate();service.wait(timeout=3)
print(json.dumps({'passed':all(row['passed'] for row in rows),'cases':rows}))
'''


def supervisor_in_image(config):
    """Real task-image process-tree tests, leaving model/GPU services untouched."""
    name = "sharedskill-offline-supervisor-" + uuid.uuid4().hex[:12]
    source = Path(__file__).with_name("agent_supervisor.py")
    def docker(*args, **kwargs):
        return environment._run(["docker", *args], config=config, **kwargs)
    started = time.time()
    try:
        docker("run", "--detach", "--name", name, *environment.provider_network_args(),
               environment.TASK_IMAGE, "tail", "-f", "/dev/null", timeout=120)
        image = json.loads(docker("inspect", name, timeout=15).stdout)[0]["Image"]
        docker("cp", str(source), name + ":/tmp/agent_supervisor.py", timeout=30)
        output = docker("exec", name, "python3", "-c", _SUPERVISOR_PROBE, timeout=40)
        proof = json.loads(output.stdout)
        return {"schema_version": 1, "started_unix": started, "finished_unix": time.time(),
                "task_image": environment.TASK_IMAGE, "actual_image_id": image,
                "supervisor_source_sha256": _sha(source.read_bytes()), "container": name,
                "scope": ("Actual task-image Agent descendants, detached/double-fork late writers and independent "
                          "service preservation; no OpenClaw/model request"),
                "no_model_calls": True, **proof}
    finally:
        docker("rm", "--force", name, timeout=30, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--supervisor", action="store_true")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    config_bytes = args.config.read_bytes()
    config = load_config(args.config)
    output = Path(config["runtime_dir"]) / "validation"
    try:
        record = offline_tasks(config)
    except Exception as error:
        record = {"passed": False, "observed_at": datetime.now(UTC).isoformat(),
                  "error": f"{type(error).__name__}: {error}", "no_model_calls": True}
    record["config_snapshot_sha256"] = _sha(config_bytes)
    record["config_stable_during_audit"] = args.config.read_bytes() == config_bytes
    record["public_config"] = public_config(config)
    record["passed"] = record["passed"] and record["config_stable_during_audit"]
    write_json(output / "offline30.json", record)
    if args.supervisor:
        try:
            proof = supervisor_in_image(config)
        except Exception as error:
            proof = {"passed": False, "observed_at": datetime.now(UTC).isoformat(),
                     "error": f"{type(error).__name__}: {error}", "no_model_calls": True}
        write_json(output / "supervisor-task-image.json", proof)
        print(json.dumps({"offline_passed": record["passed"], "supervisor_passed": proof["passed"],
                          "output": str(output)}))
        return 0 if record["passed"] and proof["passed"] else 1
    else:
        print(json.dumps({"offline_passed": record["passed"], "output": str(output / "offline30.json")}))
        return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
