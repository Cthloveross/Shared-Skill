import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.injection.cohort_injection import FIELDS, inject_uploaded
from scripts.injection.platform_payload import DEFAULT_ROOT, LEGACY_FIELDS, MODEL_FIELDS, REVISION, compile_payload, load_catalog


def test_compile_preserves_selected_original_values_and_pins_complete_case():
    original_bytes = (DEFAULT_ROOT / "data_exfil_file.json").read_bytes()
    case = load_catalog()["data_exfil_file"]
    payload = compile_payload()
    assert payload["text"] == case["message"]
    assert payload["insertion_mode"] == "message_body"
    assert payload["payload_revision"] == REVISION
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


def test_twelve_messages_have_distinct_canaries_and_explicit_measurement_scope():
    cases = load_catalog()
    assert len({case['target_behavior'] for case in cases.values()}) == len(cases) == 12
    assert len({case['message'] for case in cases.values()}) == 12
    for case_id, case in cases.items():
        payload = compile_payload(case_id)
        assert 0 < len(payload['text']) <= 400
        assert 'PLT-CHECK' not in payload['text'] and 'workbook' not in payload['text'].lower()
        assert all(key not in case for key in ('constraint', 'action', 'success'))
        assert payload['parameters']['synthetic'] is True
        assert payload['parameters']['execution'] == 'record_only'
        measure = payload['behavioral_measurement']
        if case_id == 'data_exfil_file':
            assert measure['status'] == 'implemented'
            assert measure['observer'] == 'scripts/injection/platform_observer.py'
            assert case['marker'] == 'PLT-CHECK-103'  # Existing observer identity, metadata only.
            assert case['parameters']['destination'] == 'fixture://logs/synthetic-audit'
            assert case['parameters']['record'] == {'timestamp': 'SYNTHETIC_TIMESTAMP', 'arguments': ['SYNTHETIC_ARGUMENT']}
        else:
            assert measure['status'] == 'text_only' and measure['asr'] == 'unknown'
            assert measure['observer'] is None


def fixture_root(tmp_path):
    root = tmp_path / "injection"
    shutil.copytree(DEFAULT_ROOT, root)
    return root


def repin(root, case):
    path = root / (case['id'] + '.json')
    path.write_text(json.dumps(case))
    catalog = json.loads((root / 'catalog.json').read_text())
    next(row for row in catalog['cases'] if row['id'] == case['id'])['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / 'catalog.json').write_text(json.dumps(catalog))


def test_legacy_four_fields_are_compatible_without_message_body_mode(tmp_path):
    root = fixture_root(tmp_path)
    for case in load_catalog(root).values():
        for key in ('message', 'payload_revision', 'schema_version'):
            case.pop(key)
        case.update(constraint='Synthetic constraint.', action='Record a test step.', success='Untrusted historical claim.')
        repin(root, case)
    catalog = json.loads((root / 'catalog.json').read_text())
    catalog.update(schema_version=1, payload_revision='platform-requirement-v2')
    (root / 'catalog.json').write_text(json.dumps(catalog))
    case = load_catalog(root)['data_exfil_file']
    payload = compile_payload(root=root)
    assert payload['text'] == json.dumps({key: case[key] for key in LEGACY_FIELDS}, ensure_ascii=False, separators=(',', ':'))
    assert 'insertion_mode' not in payload
    assert payload['provenance']['source_function_dispatched'] is False


@pytest.mark.parametrize('mutation', ['missing_message', 'empty_message', 'oversize', 'legacy_mixed', 'revision'])
def test_invalid_new_schema_fails_closed(tmp_path, mutation):
    root = fixture_root(tmp_path)
    case = load_catalog(root)['data_exfil_file']
    if mutation == 'missing_message':
        case.pop('message')
    elif mutation == 'empty_message':
        case['message'] = ' '
    elif mutation == 'oversize':
        case['message'] = 'a' * 401
    elif mutation == 'legacy_mixed':
        case['success'] = 'Pretended success'
    else:
        case['payload_revision'] = 'old'
    repin(root, case)
    with pytest.raises(ValueError):
        compile_payload(root=root)


def test_compile_cli_writes_only_new_output_and_never_executes_tools(tmp_path):
    target = tmp_path / 'payload.json'
    command = [sys.executable, '-B', '-m', 'scripts.injection.platform_payload', '--case', 'data_exfil_file', '--output', str(target)]
    command.extend(["--message-id", "msg_501"])
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    original = target.read_bytes()
    assert json.loads(original) == {**compile_payload(), "target_message_id": "msg_501"}
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and 'FileExistsError' in result.stderr
    assert target.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == ['payload.json']


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
        body = json.loads(modified['turns'][0][field][0]['content'])
        expected = json.loads(before['turns'][0][field][0]['content'])
        assert body == {**expected, 'content': payload['text'] + '\n\n' + expected['content']}
    assert "save_data" not in [x["function"]["name"] for x in modified["turns"][0]["tool_calls"]]


def test_actual_summarizer_formatter_keeps_full_selected_case():
    from evolve_server.pipeline.summarizer import _build_session_payload

    payload = compile_payload()
    modified, _ = inject_uploaded(real_linked_result_fixture(), payload, "/slack/messages/get")
    modified["_tool_observation_content_max_chars"] = 4096
    formatted = _build_session_payload(modified)
    for field in ("tool_results", "tool_observations"):
        content = formatted["interactions"][0][field][0]["content"]
        assert json.loads(content)['content'] == payload['text'] + '\n\nOriginal message.'
        assert 'parameters' not in json.loads(content)


def test_actual_summarizer_legacy_default_remains_240_characters():
    from evolve_server.pipeline.summarizer import _build_session_payload

    payload = compile_payload()
    modified, _ = inject_uploaded(real_linked_result_fixture(), payload, "/slack/messages/get")
    formatted = _build_session_payload(modified)
    for field in ("tool_results", "tool_observations"):
        content = formatted["interactions"][0][field][0]["content"]
        assert content == modified['turns'][0][field][0]['content'][:240] + '...'
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
    assert json.loads(actual['tool_results'][0]['content'])['content'].startswith(payload['text'] + '\n\n')
