"""Validate this cohort's frozen inputs around the unchanged A/B executor.

The cohort freezes absolute workspace paths and its own complete tree manifests.
Those descriptors cannot be passed to the upstream relative-path provenance
validator. This adapter checks the cohort contract explicitly, then retains the
controller's existing execution identities, cache checks and scoring recovery.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
from pathlib import Path
import re
import runpy
import subprocess
import sys
import threading
from types import FunctionType

from scripts.injection import e2e



POLICY = "cohort-frozen-task-and-workspace-v1"
BINDING_KEY = "cohort_validation_binding"
WORKSPACE = r"(## Workspace Path\s*\n\s*```[^\n]*\n)[^\n]+"
WEB_ONLY = {"01_Productivity_Flow_task_1_arxiv_digest", "01_Productivity_Flow_task_9_scp_crawl"}


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_validation_binding(source_run: Path, runtime_path: Path | None = None) -> dict:
    """Bind the new protocol to the original immutable cohort descriptors."""
    source_run = source_run.resolve()
    paths = {name: source_run / name for name in ("config.runtime.json", "frozen-tasks.json")}
    if runtime_path is None:
        runtime_path = Path(e2e.load_components()[5].__file__)
    runtime_path = runtime_path.resolve()
    return {"policy": POLICY, "source_run": str(source_run),
            "runtime": {"path": str(runtime_path), "sha256": _hash_file(runtime_path)},
            "files": {name: _hash_file(path) for name, path in paths.items()}}


def _bound_tasks(binding: dict) -> dict[str, dict]:
    if binding.get("policy") != POLICY or set(binding.get("files", {})) != {
            "config.runtime.json", "frozen-tasks.json"}:
        raise ValueError("Unknown cohort input validation binding")
    runtime = binding.get("runtime", {})
    runtime_path = Path(runtime.get("path", ""))
    if not runtime_path.is_absolute() or _hash_file(runtime_path) != runtime.get("sha256"):
        raise ValueError("Frozen task runtime source changed")
    root = Path(binding["source_run"])
    if not root.is_absolute():
        raise ValueError("Cohort validation source must be absolute")
    for name, expected in binding["files"].items():
        if _hash_file(root / name) != expected:
            raise ValueError(f"Bound cohort descriptor changed: {name}")
    tasks = e2e.read_json(root / "config.runtime.json")["all_tasks"]
    frozen = e2e.read_json(root / "frozen-tasks.json")
    if len(tasks) != len(frozen) or len({task["task_id"] for task in tasks}) != len(tasks):
        raise ValueError("Frozen cohort task identities differ")
    for task, record in zip(tasks, frozen, strict=True):
        if any(task.get(key) != value for key, value in record.items()):
            raise ValueError(f"Frozen cohort task descriptor differs: {task['task_id']}")
    return {task["task_id"]: task for task in tasks}


def validate_cohort_inputs(binding: dict, task: dict, *, expected_tasks: dict | None = None) -> dict:
    """Verify source bytes, the sole path substitution and all workspace bytes."""
    expected = (expected_tasks if expected_tasks is not None else _bound_tasks(binding)).get(task["task_id"])
    if expected != task:
        raise ValueError("A/B task differs from its bound cohort descriptor")
    original = Path(task["original_source_path"])
    source = Path(task["source_path"])
    if source.is_symlink() or original.is_symlink():
        raise ValueError("Symlink in frozen task source")
    original_bytes = original.read_bytes()
    original_hash = e2e.sha256(original_bytes)
    if (original_hash != task["original_source_sha256"]
            or original_hash != task["source_sha256"]):
        raise ValueError("Original cohort task source changed")
    expected_text = original_bytes.decode()
    manifest = task.get("input_manifest")
    if task.get("input_sha256"):
        workspace = source.parent / "task-workspace"
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("Frozen cohort workspace is missing or a symlink")
        actual = e2e.tree_manifest(workspace)
        if (not manifest or actual["sha256"] != task["input_sha256"]
                or actual["sha256"] != manifest.get("sha256")
                or actual["entries"] != manifest.get("entries")):
            raise ValueError("Frozen cohort input tree changed")
        expected_text, count = re.subn(WORKSPACE, lambda match: match.group(1) + str(workspace),
                                       expected_text, count=1)
        if count != 1:
            raise ValueError("Cohort task lacks its original workspace declaration")
    elif task["task_id"] not in WEB_ONLY or manifest is not None:
        raise ValueError("Only the frozen upstream web tasks may lack a workspace")
    if source.read_bytes() != expected_text.encode():
        raise ValueError("Frozen cohort task prompt changed")
    return {"policy": POLICY, "task_id": task["task_id"], "source_path": str(source),
            "source_sha256": _hash_file(source), "original_source_sha256": original_hash,
            "input_sha256": task.get("input_sha256"), "validated": True}


class _RuntimeProcesses:
    """Redirect only the unchanged executor's exact Python child command."""

    def __init__(self, runtime_path: Path):
        self.runtime_path = str(runtime_path)

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, command, *args, **kwargs):
        if list(command[:2]) == [sys.executable, self.runtime_path]:
            command = [sys.executable, "-m", "scripts.injection.cycle_validation", "--runtime-child",
                       self.runtime_path, *command[2:]]
        elif list(command[:3]) == [sys.executable, "-m", "scripts.wildclawbench.task_runtime"]:
            command = [sys.executable, "-m", "scripts.injection.cycle_validation", "--runtime-child",
                       self.runtime_path, *command[3:]]
        if list(command[:3]) == [sys.executable, "-m", "scripts.injection.cycle_validation"]:
            kwargs["cwd"] = e2e.ROOT
        return subprocess.run(command, *args, **kwargs)


@contextmanager
def execution_bridge(runtime, binding: dict):
    """Carry explicit cohort provenance into the executor's fresh Python child."""
    _bound_tasks(binding)
    path = Path(runtime.__file__).resolve()
    if str(path) != binding["runtime"]["path"]:
        raise ValueError("Execution bridge runtime differs from frozen source binding")
    original = runtime.execute_task
    namespace = dict(original.__globals__, subprocess=_RuntimeProcesses(path))
    bridged = FunctionType(original.__code__, namespace, original.__name__,
                           original.__defaults__, original.__closure__)
    bridged.__kwdefaults__ = original.__kwdefaults__

    def execute_task(task_id, skill_dir, output_dir, config):
        if BINDING_KEY not in config:
            # Endpoint sandbox inputs have their own explicit canary/input contract.
            return original(task_id, skill_dir, output_dir, config)
        if config[BINDING_KEY] != binding:
            raise ValueError("Execution bridge received a different cohort input binding")
        return bridged(task_id, skill_dir, output_dir, config)

    runtime.execute_task = execute_task
    try:
        yield
    finally:
        runtime.execute_task = original


def _runtime_child(runtime_path: Path, arguments: list[str]) -> None:
    """Install cohort validation, then run the original frozen runtime main."""
    config = e2e.read_json(Path(arguments[arguments.index("--config") + 1]))
    binding = config.get(BINDING_KEY)
    tasks = _bound_tasks(binding or {})
    runtime_path = runtime_path.resolve()
    if str(runtime_path) != binding["runtime"]["path"]:
        raise ValueError("Runtime child differs from its frozen source binding")
    task_id = arguments[arguments.index("--task") + 1]
    task = tasks.get(task_id)
    supplied = config.get("task", {})
    if (not task or supplied.get("source_path") != task["source_path"]
            or config.get("user_id") != task["user_id"]
            or supplied not in (task, {"source_path": task["source_path"]})):
        raise ValueError("Runtime child task differs from the frozen cohort binding")
    proof = validate_cohort_inputs(binding, task, expected_tasks=tasks)
    output = Path(arguments[arguments.index("--output") + 1])
    e2e.write_json(output / "runtime-child-input-validation.json", {
        **proof, "runtime": binding["runtime"], "entrypoint": str(Path(__file__).resolve())})
    from scripts.wildclawbench import provenance

    original = provenance.validate_task_inputs

    def validate(settings, frozen_task):
        if settings.get(BINDING_KEY) == binding:
            return validate_cohort_inputs(binding, frozen_task, expected_tasks=tasks)
        return original(settings, frozen_task)

    provenance.validate_task_inputs = validate
    sys.argv = [str(runtime_path), *arguments]
    from scripts.wildclawbench import task_runtime
    if Path(task_runtime.__file__).resolve() == runtime_path:
        runpy.run_module("scripts.wildclawbench.task_runtime", run_name="__main__")
    else:
        # Explicit, digest-bound child files also support isolated offline fixtures.
        runpy.run_path(str(runtime_path), run_name="__main__")


@contextmanager
def validation_adapter(controller, binding: dict):
    """Scope the cohort provenance translation to this revision's A/B calls.

    The task executor remains unchanged. The original controller receives
    the same task/config and still runs all of its cache, deadline and snapshot
    checks. Other provenance calls continue through the official validator.
    """
    from scripts.wildclawbench import provenance


    expected_tasks = _bound_tasks(binding)
    binding = copy.deepcopy(binding)
    original_run_one = controller.run_one
    original_validate = provenance.validate_task_inputs
    local = threading.local()

    def run_one(task, bank, run_dir, config, phase, number, arm="", candidate_id=""):
        if config.get(BINDING_KEY) != binding:
            raise ValueError("A/B execution lacks its frozen cohort validation binding")
        _bound_tasks(binding)  # Detect descriptor changes after adapter installation.
        proof = validate_cohort_inputs(binding, task, expected_tasks=expected_tasks)
        directory = (Path(run_dir) / "executions" / phase / f"{number:02d}"
                     / (candidate_id or "main") / (arm or "shared") / task["short_id"])
        e2e.write_json(directory / "input-validation.json", proof)
        previous = getattr(local, "validated", None)
        local.validated = (config, task, proof)
        try:
            return original_run_one(task, bank, run_dir, config, phase, number, arm, candidate_id)
        finally:
            local.validated = previous

    def validate(config, task):
        active = getattr(local, "validated", None)
        if active and active[0] is config and active[1] is task:
            return active[2]
        if active and active[1] is task and config.get(BINDING_KEY) == binding:
            # The OCR scorer validates again using the executor's copied config.
            return validate_cohort_inputs(binding, task, expected_tasks=expected_tasks)
        return original_validate(config, task)

    controller.run_one = run_one
    provenance.validate_task_inputs = validate
    try:
        from scripts.wildclawbench import task_runtime

        with execution_bridge(task_runtime, binding):
            yield
    finally:
        controller.run_one = original_run_one
        provenance.validate_task_inputs = original_validate


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "--runtime-child":
        raise SystemExit("Expected --runtime-child FROZEN_RUNTIME followed by its original arguments")
    _runtime_child(Path(sys.argv[2]), sys.argv[3:])
