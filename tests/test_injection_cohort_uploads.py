import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest


from scripts.injection.cohort_uploads import capture_cohort_upload, install_upload_archive
from sharedskill.api_server import SharedSkillAPIServer
from sharedskill.config import SharedSkillConfig
from sharedskill.object_store import LocalObjectStore
from sharedskill.skill_hub import SkillHub


def make_api(service):
    config = SharedSkillConfig(sharing_enabled=True, sharing_backend="local", sharing_group_id="day",
                               sharing_local_root=str(service / "shared-objects"))
    api = SimpleNamespace(config=config, _session_turns={})
    api._upload_session_data = SharedSkillAPIServer._upload_session_data.__get__(api)
    return api


def payload(session, turn=1):
    return {"session_id": session, "timestamp": "2026-10-17T02:00:00Z", "num_turns": 1,
            "turns": [{"turn_num": turn, "response_text": "actual response", "tool_calls": []}]}


def make_service(tmp_path):
    service = tmp_path / "services/day-05.11"
    service.mkdir(parents=True)
    (service / "records").mkdir()
    main = service.name + "-chat"
    (service / "routing.jsonl").write_text(json.dumps({
        "destination": "sharedskill_agent", "session_id": main, "path": "/runs/wcb-real/v1/chat/completions"}) + "\n")
    (service / "records/conversations.jsonl").write_text(json.dumps({
        "session_id": main, "turn": 1, "response_text": "actual response"}) + "\n")
    store = LocalObjectStore(service / "shared-objects")
    store.put_object(f"day/sessions/{main}.json", json.dumps(payload(main)).encode())
    return service, store, main


def test_multiple_auxiliary_uploads_preserved_and_main_route_selected(tmp_path):
    service, store, main = make_service(tmp_path)
    for name in ["tui-local-tts-one", "tui-qwen-two"]:
        store.put_object(f"day/sessions/{name}.json", json.dumps(payload(name)).encode())
    original = {item.key: store.get_object(item.key).read() for item in store.iter_objects()}
    session = capture_cohort_upload(None, service, {"task_id": "video-dub", "user_id": "user-01"}, "bank")
    evidence = json.loads((service / "upload-evidence.json").read_text())
    assert session["session_id"] == main
    assert session["task_id"] == "video-dub"
    assert session["skill_bank_sha256"] == "bank"
    assert evidence["object_key"] == f"day/sessions/{main}.json"
    assert len(evidence["auxiliary_uploads"]) == 2
    assert all(row["preserved"] for row in evidence["auxiliary_uploads"])
    assert original == {item.key: store.get_object(item.key).read() for item in store.iter_objects()}


@pytest.mark.parametrize("routes", [[], [{"destination": "sharedskill_agent", "session_id": "other-chat"}],
    [{"destination": "sharedskill_agent", "session_id": "day-05.11-chat"},
     {"destination": "sharedskill_agent", "session_id": "ambiguous-chat"}]])
def test_wrong_or_ambiguous_agent_route_rejected(tmp_path, routes):
    service, _, _ = make_service(tmp_path)
    (service / "routing.jsonl").write_text("".join(json.dumps(row) + "\n" for row in routes))
    with pytest.raises(ValueError, match="routing must bind exactly"):
        capture_cohort_upload(None, service, {"task_id": "task", "user_id": "user"}, "bank")


def test_matching_native_api_execution_record_is_required(tmp_path):
    service, _, _ = make_service(tmp_path)
    (service / "records/conversations.jsonl").write_text(json.dumps({"session_id": "unrelated"}) + "\n")
    with pytest.raises(ValueError, match="matching native API"):
        capture_cohort_upload(None, service, {"task_id": "task", "user_id": "user"}, "bank")


def test_capture_uploads_only_live_proven_main_session(tmp_path):
    service, store, main = make_service(tmp_path)
    store.delete_object(f"day/sessions/{main}.json")
    api = make_api(service)
    api._session_turns = {main: payload(main)["turns"], "tui-side": payload("tui-side")["turns"]}
    install_upload_archive(api, service)
    session = capture_cohort_upload(api, service, {"task_id": "task", "user_id": "user"}, "bank")
    assert session["session_id"] == main
    assert [item.key for item in store.iter_objects()] == [f"day/sessions/{main}.json"]
    manifest = [json.loads(line) for line in (service / "native-upload-versions/manifest.jsonl").read_text().splitlines()]
    assert len(manifest) == 1 and manifest[0]["session_id"] == main
    assert (service / manifest[0]["path"]).read_bytes() == store.get_object(manifest[0]["object_key"]).read()


def test_archive_keeps_exact_successful_native_versions_and_normal_writes(tmp_path):
    service = tmp_path / "service"
    api = make_api(service)
    install_upload_archive(api, service)
    install_upload_archive(api, service)
    bucket = SkillHub.object_storage_from_config(api.config)._bucket
    versions = [json.dumps(payload("main", index), ensure_ascii=False).encode() for index in [1, 2, 1]]
    for data in versions:
        bucket.put_object("day/sessions/main.json", data)
    archived = list((service / "native-upload-versions/main").glob("*.json"))
    assert len(archived) == 2
    assert {path.read_bytes() for path in archived} == set(versions)
    assert bucket.get_object("day/sessions/main.json").read() == versions[-1]
    manifest = [json.loads(line) for line in (service / "native-upload-versions/manifest.jsonl").read_text().splitlines()]
    assert len(manifest) == 3
    assert [row["sha256"] for row in manifest] == [hashlib.sha256(data).hexdigest() for data in versions]


def test_archive_is_thread_safe_and_other_configurations_are_unchanged(tmp_path):
    service = tmp_path / "registered"
    api = make_api(service)
    install_upload_archive(api, service)
    versions = [json.dumps(payload("main", index)).encode() for index in range(24)]

    def write(data):
        SkillHub.object_storage_from_config(api.config)._bucket.put_object("day/sessions/main.json", data)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, versions))
    manifest = [json.loads(line) for line in (service / "native-upload-versions/manifest.jsonl").read_text().splitlines()]
    assert len(manifest) == 24
    assert {hashlib.sha256(data).hexdigest() for data in versions} == {row["sha256"] for row in manifest}
    unregistered = make_api(tmp_path / "unregistered")
    assert asyncio.run(unregistered._upload_session_data("main", payload("main")["turns"]))
    assert not (tmp_path / "unregistered/native-upload-versions").exists()


def test_failed_native_write_cannot_create_successful_archive_evidence(tmp_path, monkeypatch):
    service = tmp_path / "service"
    api = make_api(service)
    install_upload_archive(api, service)

    def failed_write(self, key, data):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(LocalObjectStore, "put_object", failed_write)
    assert not asyncio.run(api._upload_session_data("main", payload("main")["turns"]))
    assert not (service / "native-upload-versions/manifest.jsonl").exists()


def test_actual_0511_native_main_and_two_auxiliary_objects_recover_offline_in_copy(tmp_path):
    root = Path(__file__).resolve().parents[1]
    original = root / "experiment/reproduction/dymalskill-trajectory-e2e/runtime/runs/30x5-live-20261016-01/services/day-05.11"
    if not original.is_dir():
        pytest.skip("Original native 05.11 evidence is absent")
    service = tmp_path / "services/day-05.11"
    for name in ["routing.jsonl", "records/conversations.jsonl"]:
        destination = service / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original / name, destination)
    shutil.copytree(original / "shared-objects/day/sessions", service / "shared-objects/day/sessions")
    session = capture_cohort_upload(None, service, {"task_id": "video-dub", "user_id": "user-01"}, "bank")
    evidence = json.loads((service / "upload-evidence.json").read_text())
    assert session["session_id"] == "day-05.11-chat"
    assert len(session["turns"]) == 43
    assert len(evidence["auxiliary_uploads"]) == 2
