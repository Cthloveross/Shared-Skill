from __future__ import annotations

import json

import pytest

from scripts.wildclawbench import task_runtime


def _spec(tmp_path):
    source = tmp_path / "task.md"
    source.write_text("task source")
    return {
        "task_id": "fixture_task",
        "file_path": str(source),
        "prompt": "Produce the requested local artifact.",
        "automated_checks": "def grade(**kwargs): return {'overall_score': 1.0}",
        "warmup": "",
        "env": "",
        "timeout_seconds": 60,
    }


def test_required_shared_skill_is_an_explicit_hashed_execution_instruction(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    patched = task_runtime._variants(
        _spec(tmp_path),
        {"model_id": "local", "required_shared_skill": "artifact-finalization"},
        output,
        "http://127.0.0.1:18182/v1",
    )
    manifest = json.loads((output / "task-variant.json").read_text())
    instruction = "read /root/skills/artifact-finalization/SKILL.md"
    assert instruction in patched["execution_preamble"]
    assert instruction in patched["prompt"]
    assert instruction not in patched["task_prompt"]
    assert manifest["required_shared_skill"] == "artifact-finalization"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "two names", ""])
def test_required_shared_skill_rejects_unsafe_names(tmp_path, name):
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(ValueError, match="safe skill name"):
        task_runtime._variants(
            _spec(tmp_path),
            {"model_id": "local", "required_shared_skill": name},
            output,
            "http://127.0.0.1:18182/v1",
        )
