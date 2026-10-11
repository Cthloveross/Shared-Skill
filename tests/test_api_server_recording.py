from __future__ import annotations

from pathlib import Path

from sharedskill.api_server import SharedSkillAPIServer
from sharedskill.config import SharedSkillConfig


def test_api_server_does_not_create_record_dir_when_recording_disabled(tmp_path: Path) -> None:
    record_dir = tmp_path / "records"
    cfg = SharedSkillConfig(record_enabled=False, record_dir=str(record_dir))

    SharedSkillAPIServer(cfg)

    assert not record_dir.exists()
