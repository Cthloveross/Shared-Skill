import hashlib
import json
from pathlib import Path

import pytest

from scripts.archive import Archive, ArchiveError


@pytest.fixture
def archive(tmp_path):
    index = tmp_path / "artifacts" / "test" / "archive.json"
    index.parent.mkdir(parents=True)
    data = {
        "schema": "sharedskill-experiment-archive-v1",
        "run_id": "test",
        "roots": [{"original": "/old/repo", "mirrors": ["artifacts/test/tree", "outputs/test/tree"]}],
    }
    index.write_text(json.dumps(data))
    return Archive(index, tmp_path)


def put(archive, relative="item.json", content=b"original"):
    path = archive.repository / "artifacts/test/tree" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_resolves_exactly_one_mirror(archive):
    source = put(archive)
    assert archive.resolve("/old/repo/item.json") == source
    target = archive.repository / "outputs/test/tree/item.json"
    target.parent.mkdir(parents=True)
    source.rename(target)
    assert archive.resolve("/old/repo/item.json") == target


def test_rejects_duplicate_and_missing(archive):
    source = put(archive)
    target = archive.repository / "outputs/test/tree/item.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    with pytest.raises(ArchiveError, match="found 2"):
        archive.resolve("/old/repo/item.json")
    with pytest.raises(ArchiveError, match="found 0"):
        archive.resolve("/old/repo/missing")


def test_rejects_symlink_and_traversal(archive):
    source = put(archive)
    (source.parent / "link").symlink_to(source)
    with pytest.raises(ArchiveError, match="Symlink"):
        archive.resolve("/old/repo/link")
    with pytest.raises(ArchiveError, match="normalized absolute"):
        archive.resolve("/old/repo/../item.json")
    with pytest.raises(ArchiveError, match="No archive root"):
        archive.resolve("/old/repository/item.json")


def test_rejects_symlink_ancestor(archive):
    source = put(archive)
    (source.parent / "dir").symlink_to(source.parent, target_is_directory=True)
    with pytest.raises(ArchiveError, match="Symlink"):
        archive.resolve("/old/repo/dir/item.json")


def test_longest_root_wins_without_fallback(archive):
    put(archive, "special/item.json")
    archive.roots.insert(0, {"original": "/old/repo/special", "mirrors": ["artifacts/test/special"]})
    with pytest.raises(ArchiveError, match="found 0"):
        archive.resolve("/old/repo/special/item.json")


def test_checks_original_digest(archive):
    source = put(archive)
    ref = {"path": "/old/repo/item.json", "sha256": hashlib.sha256(b"original").hexdigest()}
    assert archive.read_reference(ref)[0] == source
    source.write_bytes(b"modified")
    with pytest.raises(ArchiveError, match="Digest mismatch"):
        archive.read_reference(ref)


def test_verify_uses_frozen_proof_without_importing_controllers(archive):
    content = b"frozen"
    put(archive, "source.py", content)
    ref = {"path": "/old/repo/source.py", "sha256": hashlib.sha256(content).hexdigest()}
    audit = json.dumps({"proof": {"science_complete": True, "requested_rounds": 3, "evidence": [ref, ref]}}).encode()
    put(archive, "audit.json", audit)
    archive.data["audit"] = {"path": "/old/repo/audit.json", "sha256": hashlib.sha256(audit).hexdigest()}
    result = archive.verify()
    assert result["verified"]
    assert result["audit_evidence_references"] == 2
    assert result["unique_files_verified"] == 2
    assert result["requested_rounds"] == 3


def test_rejects_conflicting_digests_in_proof(archive):
    put(archive)
    good = {"path": "/old/repo/item.json", "sha256": hashlib.sha256(b"original").hexdigest()}
    audit = json.dumps({"proof": {"evidence": [good, {**good, "sha256": "0" * 64}]}}).encode()
    put(archive, "audit.json", audit)
    archive.data["audit"] = {"path": "/old/repo/audit.json", "sha256": hashlib.sha256(audit).hexdigest()}
    result = archive.verify()
    assert not result["verified"]
    assert "Conflicting" in result["errors"][0]
