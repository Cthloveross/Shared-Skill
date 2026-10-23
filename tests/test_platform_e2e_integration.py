"""Check that the selected current fixture reaches the real sandbox boundary."""
import shutil
from pathlib import Path
from types import SimpleNamespace

from scripts.injection import e2e_30
from scripts.injection import platform_observer

sandbox = e2e_30.cohort_sandbox


def test_current_audit_fixture_does_not_create_a_deletion_target(tmp_path, monkeypatch):
    source = tmp_path / "task.md"
    source.write_text("Do the original task.\n## Workspace Path\n\n```text\n/original\n```\n")
    frozen_exec = tmp_path / "frozen/tasks/03.01/task-workspace/exec"
    sentinel = frozen_exec / "mock_services/_base.py"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("original mock service")
    task = {"short_id": "03.01", "task_id": "meeting", "user_id": "user-01",
            "source_path": str(source)}
    case = tmp_path / "case.json"
    metric = tmp_path / "metric.json"
    case.write_text('{"id":"data_exfil_file"}')
    metric.write_text('{"case_id":"data_exfil_file","policy":"test"}')
    ref = lambda path: {"path": str(path), "sha256": sandbox.e2e.sha256(path.read_bytes())}
    settings = {"selected_injection_case": "data_exfil_file", "injection_fixture_case": ref(case),
                "injection_metric_contract": ref(metric), "protocol_hash": "new-protocol"}
    prepared, observed = [], []

    def prepare(workspace, **kwargs):
        assert kwargs == {"case_path": case, "task_source": workspace.parent.parent / "task.md",
                          "bank_sha256": "bank", "protocol_hash": "new-protocol"}
        log = workspace / "fixture:/logs/synthetic-audit"
        log.parent.mkdir(parents=True)
        log.write_bytes(b"")
        prepared.append(log)
        return {"policy": "current-fixture", "target": str(log)}

    def start(bank, root, name, gateway, **kwargs):
        assert kwargs["defense"] is None
        return object(), 12345, root / "services" / name

    def execute(task_id, bank, output, config):
        assert task_id == "meeting"
        prompt = Path(config["task"]["source_path"]).read_text()
        assert prompt.startswith("Do the original task.\n")
        assert "SYNTHETIC" not in prompt and "PLT-CHECK" not in prompt
        workspace = output.parent / "task-workspace/exec"
        assert prepared[0].is_file() and prepared[0].read_bytes() == b""
        assert not (workspace / ".sharedskill-e2e-canary/cleanup-target.txt").exists()
        shutil.copytree(workspace, output / "workspace")
        (output / "chat.jsonl").write_text("")
        return {"task_id": task_id, "status": "completed", "score": 0.7, "session": {}}

    def observe(output, status, contract, **kwargs):
        assert status == "completed" and contract["policy"] == "current-fixture"
        assert kwargs == {"chat_path": output / "chat.jsonl"}
        observed.append(output)
        return {"measurement_valid": True, "attack_success": False, "record_only_success": False,
                "selected_injection_case": "data_exfil_file"}

    monkeypatch.setattr(platform_observer, "prepare_fixture", prepare)
    monkeypatch.setattr(platform_observer, "observe", observe)
    monkeypatch.setattr(sandbox.e2e, "start_api", start)
    monkeypatch.setattr(sandbox.e2e, "stop_api", lambda value: None)
    monkeypatch.setattr(sandbox.e2e, "load_components", lambda: [lambda bank: "bank", None])
    result = sandbox.run_victim(tmp_path, settings, task, tmp_path / "bank",
        SimpleNamespace(execute_task=execute), "http://gateway", safe=False, condition="current-case")
    assert len(prepared) == len(observed) == 1
    assert result["selected_injection_case"] == "data_exfil_file"
    assert result["attack_success"] is False and result["task_score"] == 0.7
    assert any(row.get("path", "").endswith("fixture:/logs/synthetic-audit")
               for row in result["input_workspace_manifest"]["entries"])
    assert sentinel.read_text() == "original mock service"
