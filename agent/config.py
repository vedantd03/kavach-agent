"""Agent configuration.  Env vars only (rule 8) - never hardcode secrets or paths.

A ``.env`` file next to the repo root is loaded on import if present, so the demo
works without exporting variables by hand.  Real values always come from the
process environment; ``.env`` only fills gaps.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from detect_core.contracts import (
    DEFAULT_EXCLUDE_DIRS,
    DEFAULT_INCLUDE_TYPES,
    DEFAULT_MAX_FILE_MB,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=value lines, '#' comments, no export/quoting games."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(REPO_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = _env(name)
    if not raw:
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Config:
    server_url: str = field(default_factory=lambda: _env("SERVER_URL", "http://localhost:8000").rstrip("/"))
    device_id: str = field(default_factory=lambda: _env("DEVICE_ID", "LAPTOP-01"))
    device_token: str = field(default_factory=lambda: _env("DEVICE_TOKEN"))
    agent_version: str = field(default_factory=lambda: _env("AGENT_VERSION", "0.1.0"))
    db_path: str = field(default_factory=lambda: _env("AGENT_DB", "agent.db"))

    poll_interval_sec: float = field(default_factory=lambda: _env_float("POLL_INTERVAL_SEC", 10.0))
    progress_interval_sec: float = field(default_factory=lambda: _env_float("PROGRESS_INTERVAL_SEC", 5.0))
    heartbeat_interval_sec: float = field(default_factory=lambda: _env_float("HEARTBEAT_INTERVAL_SEC", 30.0))
    processor_threads: int = field(default_factory=lambda: _env_int("PROCESSOR_THREADS", 2))

    # Crawl defaults; a server SCAN command overrides these per scan.
    include_types: list[str] = field(default_factory=lambda: _env_list("INCLUDE_TYPES", DEFAULT_INCLUDE_TYPES))
    exclude_dirs: list[str] = field(default_factory=lambda: _env_list("EXCLUDE_DIRS", DEFAULT_EXCLUDE_DIRS))
    max_file_mb: int = field(default_factory=lambda: _env_int("MAX_FILE_MB", DEFAULT_MAX_FILE_MB))

    # HTTP behaviour (api.md 1).
    retry_attempts: int = field(default_factory=lambda: _env_int("RETRY_ATTEMPTS", 5))
    retry_backoff_sec: list[float] = field(
        default_factory=lambda: [float(x) for x in _env_list("RETRY_BACKOFF_SEC", ["1", "2", "4", "8", "16"])]
    )
    timeout_poll_sec: float = field(default_factory=lambda: _env_float("TIMEOUT_POLL_SEC", 15.0))
    timeout_ocr_sec: float = field(default_factory=lambda: _env_float("TIMEOUT_OCR_SEC", 60.0))
    timeout_detect_sec: float = field(default_factory=lambda: _env_float("TIMEOUT_DETECT_SEC", 180.0))
    timeout_default_sec: float = field(default_factory=lambda: _env_float("TIMEOUT_DEFAULT_SEC", 15.0))

    #: Send ``local-*`` scan ids on /detect so CLI scans stay attributable in
    #: /admin/findings.  Turn off if the server ever rejects unknown scan ids.
    send_local_scan_id: bool = field(default_factory=lambda: _env_bool("SEND_LOCAL_SCAN_ID", True))

    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())

    @property
    def db_file(self) -> Path:
        path = Path(self.db_path)
        return path if path.is_absolute() else (REPO_ROOT / path)


def load_config() -> Config:
    """Build a Config from the current environment (re-read each call, for tests)."""
    return Config()
