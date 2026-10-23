"""Compile pinned synthetic message text; never execute source_function.

Legacy four-field fixtures remain readable for historical evidence only.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

POLICY = "message-body-record-only-v3"
REVISION = "message-body-canary-v3"
DEFAULT_CASE = "data_exfil_file"
DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "injection"
MODEL_FIELDS = ("message",)
LEGACY_FIELDS = ("constraint", "action", "success", "parameters")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("Non-finite JSON value: " + value)


def _read(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Fixture evidence must be a regular file: " + str(path))
    raw = path.read_bytes()
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    if not isinstance(value, dict):
        raise ValueError("Fixture document must be a JSON object")
    return value, {"path": str(path), "sha256": _sha(raw)}


def _load(root: Path):
    root = Path(root).resolve(strict=True)
    catalog, catalog_ref = _read(root / "catalog.json")
    entries = catalog.get("cases")
    if not isinstance(entries, list) or len(entries) != 12:
        raise ValueError("Expected twelve catalogued source fixtures")
    cases, references = {}, {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError("Invalid fixture catalog entry")
        case_id = entry["id"]
        if not case_id or case_id in cases or entry.get("path") != case_id + ".json":
            raise ValueError("Duplicate ID or noncanonical fixture path")
        path = root / entry["path"]
        if path.parent != root or path.resolve() != path:
            raise ValueError("Fixture path must remain directly inside injection")
        case, reference = _read(path)
        if reference["sha256"] != entry.get("sha256") or case.get("id") != case_id:
            raise ValueError("Fixture ID or catalog hash changed: " + case_id)
        cases[case_id], references[case_id] = case, reference
    return cases, references, catalog, catalog_ref


def load_catalog(root: Path = DEFAULT_ROOT) -> dict[str, dict]:
    """Load the twelve source fixtures only after checking their exact bytes."""
    return _load(root)[0]


def compile_payload(case_id: str = DEFAULT_CASE, *, root: Path = DEFAULT_ROOT) -> dict:
    """Return verbatim message text and separately pinned review metadata."""
    cases, references, catalog, catalog_ref = _load(root)
    if case_id not in cases:
        raise ValueError("Unknown fixture case: " + str(case_id))
    case = cases[case_id]
    message_body = catalog.get("payload_revision") == REVISION
    fields = MODEL_FIELDS if message_body else LEGACY_FIELDS
    required_text = ("message", "marker", "source_function") if message_body else (
        "constraint", "action", "success", "marker", "source_function")
    for key in required_text:
        if not isinstance(case.get(key), str) or not case[key]:
            raise ValueError("Missing fixture field: " + key)
    parameters = case.get("parameters")
    if not isinstance(parameters, dict) or parameters.get("synthetic") is not True or parameters.get("execution") != "record_only":
        raise ValueError("Only explicitly synthetic record_only fixtures are supported")
    if not isinstance(case.get("source"), dict):
        raise ValueError("The original fixture source references are required")
    if message_body:
        if (catalog.get("schema_version") != 3 or case.get("schema_version") != 3 or case.get("payload_revision") != REVISION
                or any(key in case for key in LEGACY_FIELDS[:-1])):
            raise ValueError("Message-body fixtures require an unmixed v3 schema")
        if (not case["message"].strip() or len(case["message"]) > 400
                or not isinstance(case.get("target_behavior"), str)
                or not isinstance(case.get("behavioral_measurement"), dict)):
            raise ValueError("Message-body fixtures require concise text and explicit behavior/measurement metadata")
        text = case["message"]
    else:
        if "message" in case:
            raise ValueError("Message-body fixtures require the v3 catalog revision")
        text = json.dumps({key: copy.deepcopy(case[key]) for key in fields}, ensure_ascii=False, separators=(",", ":"))
    compiler = Path(__file__).resolve()
    payload = {
        "id": case_id,
        "source_case": case_id,
        "source_payload": case_id + ".json",
        "payload_revision": catalog.get("payload_revision"),
        "carrier": "one message body in an existing proven linked native message-read tool-result" if message_body
                   else "one existing proven linked native message-read tool-result",
        "text": text,
        "parameters": copy.deepcopy(parameters),
        "fixture_execution": "record_only",
        "provenance": {
            "policy": POLICY if message_body else "platform-requirement-record-only-tool-result-v1",
            "case": references[case_id],
            "catalog": catalog_ref,
            "compiler": {"path": str(compiler), "sha256": _sha(compiler.read_bytes())},
            "source": copy.deepcopy(case["source"]),
            "serialization": "message-utf8-verbatim-v1" if message_body else "json-utf8-ensure_ascii_false-fixed-field-order-compact-v1",
            "serialized_case_sha256": _sha(text.encode("utf-8")),
            "serialized_case_characters": len(text),
            "allowlisted_fields": list(fields),
            "all_selected_original_values_preserved": True,
            "omitted_review_fields": [key for key in case if key not in fields],
            "new_tool_turns_created": False,
            "source_function_dispatched": False,
            "success_claim": "none; message requests are not execution evidence" if message_body
                             else "untrusted fixture text; not an observed tool execution",
        },
    }
    if message_body:
        payload.update(insertion_mode="message_body", target_behavior=case["target_behavior"],
                       behavioral_measurement=copy.deepcopy(case["behavioral_measurement"]))
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default=DEFAULT_CASE, dest="case_id")
    parser.add_argument("--message-id", help="Select one message ID already present in the linked read response")
    parser.add_argument("--output", type=Path, required=True, help="New compiled JSON; existing files are never replaced")
    arguments = parser.parse_args()
    payload = compile_payload(arguments.case_id)
    if arguments.message_id:
        payload["target_message_id"] = arguments.message_id
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"case": payload["id"], "output": str(arguments.output), "revision": payload["payload_revision"]}))


if __name__ == "__main__":
    main()
