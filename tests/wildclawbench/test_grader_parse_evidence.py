"""Native grader parse failures must remain unknown even when caught as zero."""
import builtins
import json
import pathlib
from pathlib import Path
import subprocess
import types
import urllib.request

import httpx
import openai
import pytest
import requests
from PIL import Image

from scripts.wildclawbench import task_runtime as runtime


ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = ROOT / "runtime/wildclawbench/upstream/WildClawBench"


@pytest.fixture
def grader(tmp_path, monkeypatch):
    for owner, name in ((json, "loads"), (httpx.Client, "send"), (requests.Session, "send"),
                        (urllib.request, "urlopen")):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-fixture")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://offline-fixture/v1")
    monkeypatch.setenv("JUDGE_MODEL", "offline-judge")
    namespace = {"_sharedskill_model_api_base_url": "http://offline-fixture/v1",
                 "_sharedskill_grading_timeout_seconds": 10}
    monitor = runtime._GRADE_MONITOR.replace("/tmp/sharedskill-judge-api.jsonl", str(tmp_path / "api.jsonl"))
    monitor = monitor.replace("/tmp/sharedskill-mock-replay.json", str(tmp_path / "mock.json"))
    exec(compile(monitor, "grading-monitor", "exec"), namespace)
    sent = []
    answer = {"text": "invalid JSON response"}
    def backend(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0,
            "model": "offline-judge", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": answer["text"]}}]})
    clients = []
    def client(**kwargs):
        value = openai.OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(backend)))
        clients.append(value)
        return value
    real_import = builtins.__import__
    def mapped_path(value, *parts):
        if str(value).startswith("/tmp_workspace"):
            value = str(tmp_path / "workspace") + str(value)[len("/tmp_workspace"):]
        return pathlib.Path(value, *parts)
    def command(args, **kwargs):
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, "120", "")
        raise RuntimeError("Audio/video tools are not part of this parser regression")
    def imports(name, *args, **kwargs):
        if name == "pathlib":
            return types.SimpleNamespace(Path=mapped_path)
        if name == "openai":
            return types.SimpleNamespace(OpenAI=client)
        if name == "subprocess":
            return types.SimpleNamespace(run=command)
        return real_import(name, *args, **kwargs)
    def run(code, text):
        answer["text"] = text
        native = {"__builtins__": {**vars(builtins), "__import__": imports}}
        exec(compile(code, "/tmp/official-automated-checks.py", "exec"), native)
        scores = native["grade"]()
        namespace["_finalize_judge_parses"]()
        events = [json.loads(line) for line in (tmp_path / "api.jsonl").read_text().splitlines()]
        return scores, events, sent
    yield run
    for value in clients:
        value.close()


def official(task):
    if not (UPSTREAM / "src/utils/task_parser.py").is_file():
        pytest.skip("Pinned official benchmark checkout is unavailable")
    config = {"experiment_root": str(ROOT), "shared_runtime_dir": str(ROOT / "runtime/wildclawbench")}
    return runtime.task_spec(task, config)


def poster_input(root):
    destination = root / "workspace/results"
    destination.mkdir(parents=True)
    Image.new("RGB", (1080, 1440), "white").save(destination / "poster.png")


def dubbing_input(root, spec):
    destination = root / "workspace/results"
    destination.mkdir(parents=True)
    ground_truth = Path(spec["workspace_path"]) / "gt/ground_truth.json"
    if not ground_truth.is_file():
        pytest.skip("Pinned official dubbing ground truth is unavailable")
    data = json.loads(ground_truth.read_text())
    (root / "workspace/gt").mkdir()
    (root / "workspace/gt/ground_truth.json").write_bytes(ground_truth.read_bytes())
    (destination / "transcript_en.txt").write_text(data["english_transcript"])
    (destination / "transcript_zh.txt").write_text("这是用于评分解析验证的中文翻译。")
    (destination / "output.mp4").write_bytes(b"0" * 100_001)


def test_official_0503_http200_invalid_json_is_unknown_not_valid_zero(tmp_path, grader):
    spec = official("05_Creative_Synthesis_task_3_product_poster")
    poster_input(tmp_path)
    scores, events, sent = grader(spec["automated_checks"], "This response is not JSON.")
    assert scores["overall_score"] == 0.0 and "llm_error" in scores  # Native swallowed exception.
    assert len(sent) == 1 and "Respond strictly in JSON" in sent[0]["messages"][0]["content"][0]["text"]
    assert any(event["kind"] == "judge_parse" for event in events)
    assert runtime._judge_diagnostics(scores, events)
    assert not runtime._scoring_recovery_eligible(events, runtime._judge_diagnostics(scores, events))


def test_official_0511_silent_extract_json_failure_is_unknown(tmp_path, grader):
    spec = official("05_Creative_Synthesis_task_11_video_en_to_zh_dub")
    dubbing_input(tmp_path, spec)
    scores, events, sent = grader(spec["automated_checks"], "Not a valid JSON translation assessment.")
    assert scores["translation_quality"] == 0.0 and "llm_error" not in scores
    assert len(sent) == 1 and "Return ONLY valid JSON" in sent[0]["messages"][0]["content"]
    assert runtime._judge_diagnostics(scores, events)


def test_native_regex_fallback_success_is_not_misclassified(tmp_path, grader):
    spec = official("05_Creative_Synthesis_task_11_video_en_to_zh_dub")
    dubbing_input(tmp_path, spec)
    scores, events, _ = grader(spec["automated_checks"],
        'Assessment follows: {"accuracy": 0.0, "fluency": 0.0, "terminology": 0.0, "style": 0.0}')
    assert scores["translation_quality"] == 0.0
    assert runtime._judge_diagnostics(scores, events) == []


def test_valid_zero_from_official_grader_remains_zero(tmp_path, grader):
    spec = official("05_Creative_Synthesis_task_3_product_poster")
    poster_input(tmp_path)
    scores, events, _ = grader(spec["automated_checks"],
        '```json\n{"content_completeness": 0, "feature_highlighting": 0, "design_impact": 0}\n```')
    assert scores["overall_score"] == 0.0
    assert runtime._judge_diagnostics(scores, events) == []


def test_native_scalar_judge_does_not_acquire_a_json_requirement(grader):
    code = '''
def grade():
    from openai import OpenAI
    import os
    client = OpenAI(api_key='fixture', base_url=os.environ['OPENROUTER_BASE_URL'])
    answer = client.chat.completions.create(model='fixture', messages=[{'role':'user','content':'Reply with the number only.'}])
    return {'overall_score': float(answer.choices[0].message.content)}
'''
    scores, events, _ = grader(code, "0.0")
    assert scores["overall_score"] == 0.0 and runtime._judge_diagnostics(scores, events) == []


def test_unrelated_file_parse_error_is_not_model_parse_evidence(grader):
    code = '''
def grade():
    from openai import OpenAI
    import os, json
    client = OpenAI(api_key='fixture', base_url=os.environ['OPENROUTER_BASE_URL'])
    client.chat.completions.create(model='fixture', messages=[{'role':'user','content':'Explain.'}])
    try: json.loads('broken task-file data')
    except json.JSONDecodeError: pass
    return {'overall_score': 0.0}
'''
    scores, events, _ = grader(code, "A valid freeform explanation.")
    assert runtime._judge_diagnostics(scores, events) == []
