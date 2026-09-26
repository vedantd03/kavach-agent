"""Configuration: env-only, and correct paths in both source and frozen builds."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

import agent.config as config_module


def reload_config(monkeypatch: pytest.MonkeyPatch):
    """Re-import the module so ``_base_dir()`` runs again under the patched state."""
    module = importlib.reload(config_module)
    monkeypatch.setattr("agent.config", module, raising=False)
    return module


def test_base_dir_is_the_repo_root_from_source():
    module = importlib.reload(config_module)
    assert module.REPO_ROOT == Path(config_module.__file__).resolve().parent.parent
    assert (module.REPO_ROOT / "agent").is_dir()


def test_base_dir_follows_the_executable_when_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A one-file build unpacks to a temp dir; .env and agent.db must not go there."""
    exe_dir = tmp_path / "shipped"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "kavach-agent.exe"))

    module = reload_config(monkeypatch)
    try:
        assert module.REPO_ROOT == exe_dir
        monkeypatch.setenv("AGENT_DB", "agent.db")
        assert module.Config().db_file == exe_dir / "agent.db"
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)


def test_absolute_agent_db_is_used_as_is(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target = tmp_path / "elsewhere" / "kavach.db"
    monkeypatch.setenv("AGENT_DB", str(target))
    assert config_module.Config().db_file == target


def test_dotenv_only_fills_gaps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("SERVER_URL=http://from-dotenv:8000\nDEVICE_ID=FROM-DOTENV\n", encoding="utf-8")
    monkeypatch.setenv("DEVICE_ID", "FROM-ENVIRONMENT")
    monkeypatch.delenv("SERVER_URL", raising=False)

    config_module._load_dotenv(env_file)
    config = config_module.Config()
    assert config.server_url == "http://from-dotenv:8000"     # gap filled
    assert config.device_id == "FROM-ENVIRONMENT"             # environment wins


def test_missing_dotenv_is_not_an_error(tmp_path: Path):
    config_module._load_dotenv(tmp_path / "nope.env")          # must not raise


def test_trailing_slash_is_stripped_from_server_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SERVER_URL", "http://192.168.1.20:8000/")
    assert config_module.Config().server_url == "http://192.168.1.20:8000"


def test_list_and_bool_env_parsing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("INCLUDE_TYPES", "txt, csv ,pdf")
    monkeypatch.setenv("SEND_LOCAL_SCAN_ID", "0")
    monkeypatch.setenv("RETRY_BACKOFF_SEC", "1,2,4")
    config = config_module.Config()
    assert config.include_types == ["txt", "csv", "pdf"]
    assert config.send_local_scan_id is False
    assert config.retry_backoff_sec == [1.0, 2.0, 4.0]
