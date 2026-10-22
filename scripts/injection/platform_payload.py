"""Compile a pinned record-only fixture into one tool-result text payload.

This module only reads JSON. It does not dispatch the source_function, invent
tool turns, or treat a fixture's success statement as an observed execution.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

POLICY = "platform-requirement-record-only-tool-result-v1"
DEFAULT_CASE = "data_exfil_file"
DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "injection"
MODEL_FIELDS = ("constraint", "action", "success", "parameters")
SERIALIZATION = "json-utf8-ensure_ascii_false-fixed-field-order-compact-v1"


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
    """Return a nonempty ``text`` accepted by the real linked-result adapter.

    The four original trajectory fields are serialized in their existing order
    without changing their values. Review labels are not model instructions.
    The complete case's raw bytes and compiler are pinned in provenance.
    This is a synthetic record-only observation, not a live deletion/exfiltration
    instruction or evidence that a source_function actually ran.
    """
    cases, references, catalog, catalog_ref = _load(root)
    if case_id not in cases:
        raise ValueError("Unknown fixture case: " + str(case_id))
    case = cases[case_id]
    for key in ("constraint", "action", "success", "marker", "source_function"):
        if not isinstance(case.get(key), str) or not case[key]:
            raise ValueError("Missing fixture field: " + key)
    parameters = case.get("parameters")
    if not isinstance(parameters, dict) or parameters.get("synthetic") is not True or parameters.get("execution") != "record_only":
        raise ValueError("Only explicitly synthetic record_only fixtures are supported")
    if not isinstance(case.get("source"), dict):
        raise ValueError("The original fixture source references are required")
    visible = {key: copy.deepcopy(case[key]) for key in MODEL_FIELDS}
    text = json.dumps(visible, ensure_ascii=False, separators=(",", ":"))
    compiler = Path(__file__).resolve()
    return {
        "id": case_id,
        "source_case": case_id,
        "source_payload": case_id + ".json",
        "payload_revision": catalog.get("payload_revision"),
        "carrier": "one existing proven linked native message-read tool-result",
        "text": text,
        "parameters": copy.deepcopy(parameters),
        "fixture_execution": "record_only",
        "provenance": {
            "policy": POLICY,
            "case": references[case_id],
            "catalog": catalog_ref,
            "compiler": {"path": str(compiler), "sha256": _sha(compiler.read_bytes())},
            "source": copy.deepcopy(case["source"]),
            "serialization": SERIALIZATION,
            "serialized_case_sha256": _sha(text.encode("utf-8")),
            "serialized_case_characters": len(text),
            "allowlisted_fields": list(MODEL_FIELDS),
            "all_selected_original_values_preserved": True,
            "omitted_review_fields": [key for key in case if key not in MODEL_FIELDS],
            "new_tool_turns_created": False,
            "source_function_dispatched": False,
            "success_claim": "untrusted fixture text; not an observed tool execution",
        },
    }
