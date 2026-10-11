from __future__ import annotations

from pathlib import Path

from sharedskill.config_store import ConfigStore


def test_to_sharedskill_config_maps_record_settings(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    record_dir = tmp_path / "records"
    store.save(
        {
            "claw_type": "none",
            "configure_openclaw": False,
            "record": {"enabled": False, "dir": str(record_dir)},
        }
    )

    cfg = store.to_sharedskill_config()

    assert cfg.record_enabled is False
    assert cfg.record_dir == str(record_dir)


def test_default_config_store_can_use_env_config_file(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "sharedskill.yaml"
    monkeypatch.setenv("SHAREDSKILL_CONFIG_FILE", str(config_file))

    store = ConfigStore()

    assert store.config_file == config_file
