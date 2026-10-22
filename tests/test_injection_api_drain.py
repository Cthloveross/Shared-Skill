"""Late proxy replies must finish before immutable Day uploads are sealed."""
import threading
import time
from types import SimpleNamespace

import pytest
from scripts.injection import e2e


def test_stop_waits_for_actual_late_native_upload(tmp_path):
    released = threading.Event()
    native = tmp_path / "shared-objects/day/sessions/example.json"
    native.parent.mkdir(parents=True)
    binding = {key: "bound-" + key for key in
               ("run_id", "execution_id", "protocol_hash", "bank_sha256", "catalog_sha256", "binding_sha256")}
    e2e.write_json(tmp_path / "execution-binding.json", binding)
    e2e.write_json(tmp_path / "upload-evidence.json", {"object_key": "day/sessions/example.json"})

    def late_reply():
        released.wait()
        time.sleep(.025)
        native.write_text("final native response, still not an Agent execution")
        (tmp_path / "records").mkdir()
        (tmp_path / "records/conversations.jsonl").write_text("actual final record\n")
        (tmp_path / "native-upload-versions").mkdir()
        (tmp_path / "native-upload-versions/manifest.jsonl").write_text("actual final upload epoch\n")

    worker = threading.Thread(target=late_reply)
    worker.start()
    api = SimpleNamespace(_thread=worker, stop=released.set,
                          config=SimpleNamespace(record_dir=str(tmp_path / "records")))
    e2e.stop_api(api, drain_timeout_seconds=1)
    assert not worker.is_alive() and native.is_file()
    proof = e2e.read_json(tmp_path / "native-api-drain.json")
    assert proof["status"] == "completed" and proof["server_thread_alive"] is False
    assert proof["new_requests_started"] == 0 and proof["agent_budget_extended"] is False
    assert proof["binding_identity"] == binding
    assert proof["final_files"]["native_object"]["sha256"] == e2e.sha256(native.read_bytes())
    assert set(proof["final_files"]) == {"native_object", "records", "native_upload_manifest"}


def test_unfinished_proxy_cannot_claim_a_completed_drain(tmp_path):
    released = threading.Event()
    worker = threading.Thread(target=released.wait)
    worker.start()
    api = SimpleNamespace(_thread=worker, stop=lambda: None,
                          config=SimpleNamespace(record_dir=str(tmp_path / "records")))
    try:
        with pytest.raises(RuntimeError, match="has not drained"):
            e2e.stop_api(api, drain_timeout_seconds=.01)
        assert e2e.read_json(tmp_path / "native-api-drain.json")["status"] == "incomplete"
    finally:
        released.set()
        worker.join(timeout=1)
