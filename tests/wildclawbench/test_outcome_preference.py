"""Blind outcome comparator uses the judge route and exact evidence cache."""
import copy
import json

import httpx
import pytest

from scripts.wildclawbench import run


@pytest.fixture
def comparison():
    old = [{"task_id": "03_social", "short_id": "03.03", "skill_bank_sha256": "s0-digest",
            "score": .8995, "timed_out": False, "tool_errors": 0,
            "session": {"messages": [{"role": "assistant", "content": "original completion"}]}}]
    new = copy.deepcopy(old)
    new[0].update(score=.903, skill_bank_sha256="candidate-digest")
    config = {"model_id": "Qwen3.5-35B-A3B", "model_revision": "fixed", "max_tokens": 4096,
              "enable_thinking": True, "context_length": 131072,
              "local_judge_enable_thinking": False, "local_judge_max_tokens": 4096,
              "local_api_base_url": "http://local-gateway:19198/v1", "local_api_key": "fixture"}
    return old, new, config


def reply(content='{"preference":"tie","reason":"No demonstrated improvement"}', finish="stop"):
    return httpx.Response(200, request=httpx.Request("POST", "http://fixture/v1/chat/completions"),
        json={"choices": [{"finish_reason": finish, "message": {"content": content}}]})


def test_local_comparator_uses_separate_judge_policy_and_completed_cache(tmp_path, monkeypatch, comparison):
    old, new, config = comparison
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs))
        return reply()
    monkeypatch.setattr(run.httpx, "post", request)
    assert run.outcome_preference(old, new, tmp_path, config) == "tie"
    assert calls[0][0].endswith("/local-judge/v1/chat/completions")
    assert run.outcome_preference(old, new, tmp_path, config) == "tie"
    assert len(calls) == 1
    receipt = json.loads((tmp_path / "03.03.json").read_text())
    assert receipt["comparison_identity"]["local_judge_settings"] == {
        "enable_thinking": False, "max_tokens": 4096}


@pytest.mark.parametrize("change", ["judge", "bank", "evidence", "preference"])
def test_changed_identity_or_verdict_cannot_reuse_cache(tmp_path, monkeypatch, comparison, change):
    old, new, config = comparison
    monkeypatch.setattr(run.httpx, "post", lambda *a, **kw: reply())
    run.outcome_preference(old, new, tmp_path, config)
    monkeypatch.setattr(run.httpx, "post", lambda *a, **kw: pytest.fail("No automatic new comparison allowed"))
    if change == "judge":
        config["local_judge_max_tokens"] = 512
    elif change == "bank":
        new[0]["skill_bank_sha256"] = "different-bank-digest"
    elif change == "evidence":
        old[0]["session"]["messages"][0]["content"] = "changed baseline output"
    else:
        path = tmp_path / "03.03.json"
        receipt = json.loads(path.read_text())
        receipt["preference"] = "candidate"
        path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="Cached outcome"):
        run.outcome_preference(old, new, tmp_path, config)


@pytest.mark.parametrize("content,finish", [(None, "length"), (None, "stop"), ("", "stop"),
    ('{"preference":"tie"}', "length"), ("not JSON", "stop"), ('{"preference":"unknown"}', "stop")])
def test_empty_truncated_or_invalid_result_remains_unknown_without_cache(tmp_path, monkeypatch, comparison, content, finish):
    old, new, config = comparison
    monkeypatch.setattr(run.httpx, "post", lambda *a, **kw: reply(content, finish))
    with pytest.raises(ValueError):
        run.outcome_preference(old, new, tmp_path, config)
    assert not (tmp_path / "03.03.json").exists()


def test_external_judge_retains_its_explicit_route(tmp_path, monkeypatch, comparison):
    old, new, config = comparison
    config = {key: value for key, value in config.items() if not key.startswith("local_judge_")}
    config.update(external_judge=True, judge_model_id="external-test")
    calls = []
    def request(url, **kwargs):
        calls.append(url)
        return reply()
    monkeypatch.setattr(run.httpx, "post", request)
    assert run.outcome_preference(old, new, tmp_path, config) == "tie"
    assert calls[0].endswith("/judge/v1/chat/completions")
