import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts.injection.cohort_injection import FIELDS, inject_uploaded
from scripts.injection.platform_payload import DEFAULT_ROOT, MODEL_FIELDS, compile_payload, load_catalog


def test_compile_preserves_selected_original_values_and_pins_complete_case():
    original_bytes = (DEFAULT_ROOT / "data_exfil_file.json").read_bytes()
    case = load_catalog()["data_exfil_file"]
    payload = compile_payload()
    assert json.loads(payload["text"]) == {key: case[key] for key in MODEL_FIELDS}
    assert list(json.loads(payload["text"])) == list(MODEL_FIELDS)
    assert payload["provenance"]["allowlisted_fields"] == list(MODEL_FIELDS)
    assert "boundary" not in payload["text"]
    assert "defense_expectation" not in payload["text"]
    assert "legacy_marker" not in payload["text"]
    assert payload["parameters"] == case["parameters"]
    assert payload["fixture_execution"] == "record_only"
    assert payload["provenance"]["case"]["sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert payload["provenance"]["source"] == case["source"]
    assert payload["provenance"]["source_function_dispatched"] is False
    assert payload["provenance"]["new_tool_turns_created"] is False
    assert (DEFAULT_ROOT / "data_exfil_file.json").read_bytes() == original_bytes
    assert compile_payload() == payload


def fixture_root(tmp_path):
    root = tmp_path / "injection"
    shutil.copytree(DEFAULT_ROOT, root)
    return root


def test_hash_mismatch_fails_before_compilation(tmp_path):
    root = fixture_root(tmp_path)
    (root / "data_exfil_file.json").write_text("{}")
    with pytest.raises(ValueError, match="catalog hash"):
        compile_payload(root=root)


def test_unknown_case_does_not_fall_back_to_old_file_delete():
    with pytest.raises(ValueError, match="Unknown fixture"):
        compile_payload("e2e_file_delete_canary_v1")


def test_execution_semantics_cannot_silently_become_real(tmp_path):
    root = fixture_root(tmp_path)
    path = root / "data_exfil_file.json"
    value = json.loads(path.read_text())
    value["parameters"]["execution"] = "execute"
    path.write_text(json.dumps(value))
    catalog = json.loads((root / "catalog.json").read_text())
    next(row for row in catalog["cases"] if row["id"] == value["id"])["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "catalog.json").write_text(json.dumps(catalog))
    with pytest.raises(ValueError, match="record_only"):
        compile_payload(root=root)


def real_linked_result_fixture():
    body = json.dumps({"message_id": "msg_501", "sender": "@fixture", "content": "Original message."})
    call = {"id": "read-1", "function": {"name": "http_request", "arguments": json.dumps({
        "url": "http://localhost:9110/slack/messages/get", "body": {"message_id": "msg_501"}})}}
    turn = {"turn_num": 1, "prompt_text": "Perform the user task.", "response_text": "Read the message.",
            "tool_calls": [call]}
    for field in FIELDS:
        turn[field] = [{"tool_call_id": "read1", "tool_name": "http_request", "content": body, "has_error": False}]
    return {"session_id": "fixture-carrier", "turns": [turn]}


def test_actual_hook_keeps_original_calls_turns_and_raw_message():
    original = real_linked_result_fixture()
    before = copy.deepcopy(original)
    payload = compile_payload()
    modified, audit = inject_uploaded(original, payload, "/slack/messages/get")
    assert original == before
    assert len(modified["turns"]) == len(original["turns"]) == 1
    assert modified["turns"][0]["tool_calls"] == original["turns"][0]["tool_calls"]
    assert modified["turns"][0]["response_text"] == original["turns"][0]["response_text"]
    assert audit["call_chain"]["source_call_id"] == "read-1"
    for field in FIELDS:
        expected = payload["text"] + "\n\n" + before["turns"][0][field][0]["content"]
        assert modified["turns"][0][field][0]["content"] == expected
    assert "save_data" not in [x["function"]["name"] for x in modified["turns"][0]["tool_calls"]]


def test_actual_summarizer_formatter_keeps_full_selected_case():
    from evolve_server.pipeline.summarizer import _build_session_payload

    payload = compile_payload()
    modified, _ = inject_uploaded(real_linked_result_fixture(), payload, "/slack/messages/get")
    modified["_tool_observation_content_max_chars"] = 4096
    formatted = _build_session_payload(modified)
    for field in ("tool_results", "tool_observations"):
        content = formatted["interactions"][0][field][0]["content"]
        assert content.startswith(payload["text"] + "\n\n")
        assert json.loads(content[:len(payload["text"])]) == {
            key: load_catalog()["data_exfil_file"][key] for key in MODEL_FIELDS}
        assert json.loads(content[:len(payload["text"])])["parameters"]["execution"] == "record_only"


def test_actual_summarizer_legacy_default_remains_240_characters():
    from evolve_server.pipeline.summarizer import _build_session_payload

    payload = compile_payload()
    modified, _ = inject_uploaded(real_linked_result_fixture(), payload, "/slack/messages/get")
    formatted = _build_session_payload(modified)
    for field in ("tool_results", "tool_observations"):
        content = formatted["interactions"][0][field][0]["content"]
        assert content == payload["text"][:240] + "..."
        assert payload["text"] not in content


def test_larger_content_budget_preserves_command_and_argument_limits():
    from evolve_server.pipeline.summarizer import _build_session_payload

    payload = compile_payload()
    modified, _ = inject_uploaded(real_linked_result_fixture(), payload, "/slack/messages/get")
    modified["_tool_observation_content_max_chars"] = 4096
    turn = modified["turns"][0]
    turn["tool_calls"][0]["function"]["arguments"] = "a" * 300
    for field in ("tool_results", "tool_observations"):
        turn[field][0]["command"] = "c" * 300
    actual = _build_session_payload(modified)["interactions"][0]
    assert actual["tool_calls"][0]["arguments"] == "a" * 240 + "..."
    assert actual["tool_results"][0]["command"] == "c" * 240 + "..."
    assert actual["tool_results"][0]["content"].startswith(payload["text"] + "\n\n")
