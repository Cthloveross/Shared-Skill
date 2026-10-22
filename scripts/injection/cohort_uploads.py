"""Bind cohort uploads to their actual Agent route and retain native versions."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from sharedskill.object_store import LocalObjectStore


_FACTORY_LOCK = threading.Lock()
_ARCHIVES: dict[int, tuple[object, Path, threading.Lock]] = {}
_SERVICE_LOCKS: dict[Path, threading.Lock] = {}
_FACTORY_INSTALLED = False


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError(f"Append-only archive differs: {path}")


def _wrap_bucket(bucket, service: Path, lock: threading.Lock) -> None:
    original = bucket.put_object

    def put_object(key, data):
        match = re.fullmatch(r"day/sessions/([A-Za-z0-9_.-]+)\.json", str(key))
        if not match:
            return original(key, data)
        content = data.read() if hasattr(data, "read") else data
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        with lock:
            result = original(key, raw)
            # The upload succeeded before these exact supplied bytes are archived.
            digest = _hash(raw)
            archive = service / "native-upload-versions" / match[1] / (digest + ".json")
            _write_once(archive, raw)
            payload = json.loads(raw)
            metadata = {
                "recorded_at": datetime.now(timezone.utc).isoformat(), "object_key": str(key),
                "session_id": match[1], "sha256": digest, "bytes": len(raw),
                "path": str(archive.relative_to(service)), "native_timestamp": payload.get("timestamp"),
                "num_turns": payload.get("num_turns"),
                "turn_numbers": [turn.get("turn_num") for turn in payload.get("turns", [])],
                "evidence": "exact_bytes_after_successful_native_put_object",
            }
            manifest = service / "native-upload-versions/manifest.jsonl"
            with manifest.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(metadata, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return result

    bucket.put_object = put_object


def install_upload_archive(api, service_dir: Path) -> None:
    """Archive each registered API's genuine writes, including closing chunks.

    Native code creates a new hub per upload. The factory dispatcher wraps only
    hubs whose configuration object is this API's exact registered object.
    """
    from sharedskill.skill_hub import SkillHub

    global _FACTORY_INSTALLED
    service = Path(service_dir).resolve()
    configured = Path(str(api.config.sharing_local_root)).resolve()
    if configured != service / "shared-objects":
        raise ValueError("Native upload archive belongs to another object store")
    with _FACTORY_LOCK:
        prior = _ARCHIVES.get(id(api.config))
        if prior and prior[1] != service:
            raise ValueError("API configuration already belongs to another upload archive")
        _ARCHIVES[id(api.config)] = prior or (api.config, service, _SERVICE_LOCKS.setdefault(service, threading.Lock()))
        if not _FACTORY_INSTALLED:
            original_factory = SkillHub.object_storage_from_config.__func__

            def factory(cls, config):
                hub = original_factory(cls, config)
                with _FACTORY_LOCK:
                    registration = _ARCHIVES.get(id(config))
                if hub is not None and registration and registration[0] is config:
                    _wrap_bucket(hub._bucket, registration[1], registration[2])
                return hub

            SkillHub.object_storage_from_config = classmethod(factory)
            _FACTORY_INSTALLED = True
    _write_once(service / "native-upload-versions/policy.json", json.dumps({
        "policy": "native-successful-object-write-exact-bytes-v1",
        "service_dir": str(service), "object_prefix": "day/sessions/",
        "scope": "registered_api_configuration_object_identity",
        "source_reconstruction": False,
    }, sort_keys=True).encode())


def capture_cohort_upload(api, service_dir: Path, task: dict, bank_hash: str) -> dict:
    """Capture the route-bound main upload; retain auxiliary sessions explicitly."""
    from scripts.injection import e2e

    service = Path(service_dir).resolve()
    routing = service / "routing.jsonl"
    records_path = service / "records/conversations.jsonl"
    routes, records = _json_lines(routing), _json_lines(records_path)
    agent_routes = [item for item in routes if item.get("destination") == "sharedskill_agent"]
    session_ids = {item.get("session_id") for item in agent_routes}
    expected = service.name + "-chat"
    if session_ids != {expected}:
        raise ValueError(f"Main Agent routing must bind exactly {expected}; found {sorted(map(str, session_ids))}")
    matched_records = [item for item in records if item.get("session_id") == expected]
    if not matched_records:
        raise ValueError("Main Agent route lacks matching native API execution records")
    store = LocalObjectStore(service / "shared-objects")
    key = f"day/sessions/{expected}.json"
    if api is not None and api._session_turns.get(expected):
        # Invoke the original native uploader for this proven main session only.
        turns = copy.deepcopy(api._session_turns[expected])
        if not asyncio.run(api._upload_session_data(expected, turns)):
            raise RuntimeError("Native main-session upload failed")
    raw_bytes = store.get_object(key).read()
    raw = json.loads(raw_bytes)
    if (raw.get("session_id") != expected or not isinstance(raw.get("turns"), list)
            or not raw["turns"] or raw.get("num_turns") != len(raw["turns"])):
        raise ValueError("Native main-session object has inconsistent identity or turn count")
    auxiliary = []
    for entry in store.iter_objects("day/sessions/"):
        if entry.key == key:
            continue
        data = store.get_object(entry.key).read()
        value = json.loads(data)
        auxiliary.append({"object_key": entry.key, "session_id": value.get("session_id"),
                          "sha256": _hash(data), "bytes": len(data), "num_turns": len(value.get("turns", [])),
                          "classification": "auxiliary_unselected_native_upload", "preserved": True})
    shared_names_path = service.parent.parent / "day/task/shared-skill-names.json"
    names = set(e2e.read_json(shared_names_path, []))
    observed_reads = e2e.observed_shared_skill_reads(raw, names)
    session = e2e.session_from_object(raw_bytes, task, bank_hash)
    evidence = {
        "object_key": key, "uploaded_sha256": _hash(raw_bytes), "uploaded_bytes": len(raw_bytes),
        "session_id": expected, "num_turns": len(raw["turns"]),
        "api_skill_reads": sorted({item.get("skill_name") for turn in raw["turns"]
                                    for item in turn.get("read_skills", []) if item.get("skill_name")}),
        "observed_shared_skill_reads": observed_reads,
        "source_task_id_added_by_experiment_manifest": task["task_id"],
        "phase_added_by_experiment_manifest": "day", "selection_policy": "unique_native_agent_route-v1",
        "routing_sha256": _hash(routing.read_bytes()), "agent_route_count": len(agent_routes),
        "api_records_sha256": _hash(records_path.read_bytes()), "matching_api_record_count": len(matched_records),
        "api_record_turn_numbers": sorted({item["turn"] for item in matched_records if isinstance(item.get("turn"), int)}),
        "auxiliary_uploads": auxiliary,
    }
    old = e2e.read_json(service / "upload-evidence.json", {})
    if old and old.get("uploaded_sha256") != evidence["uploaded_sha256"]:
        for name in ("uploaded-session.json", "native-day-session.json", "upload-evidence.json"):
            path = service / name
            if path.exists():
                _write_once(service / "capture-history" / old["uploaded_sha256"] / name, path.read_bytes())
        evidence["previous_capture_sha256"] = old["uploaded_sha256"]
    e2e.write_json(service / "uploaded-session.json", raw)
    e2e.write_json(service / "native-day-session.json", session)
    e2e.write_json(service / "upload-evidence.json", evidence)
    _write_once(service / "captured-native-uploads" / (_hash(raw_bytes) + ".json"), raw_bytes)
    return session
