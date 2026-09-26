"""Small shared helpers: time, hashing, logging, error redaction.

Rule 1: nothing here may ever return or log file content or a raw identifier.
``safe_error`` is the only approved way to turn an exception into a stored
string - it keeps the class name and a truncated message and nothing else.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

HASH_CHUNK_BYTES = 1024 * 1024
MAX_ERROR_CHARS = 200


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    """ISO-8601 UTC with a trailing Z, e.g. ``2026-09-26T09:30:00Z``."""
    return iso(utc_now())


def iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_mtime(mtime: float) -> str:
    return iso(datetime.fromtimestamp(mtime, tz=timezone.utc))


def local_scan_id() -> str:
    """CLI scans use ``local-<uuid4>`` so the server never sees them (api.md 1)."""
    return f"local-{uuid.uuid4()}"


def is_local_scan(scan_id: str) -> bool:
    return scan_id.startswith("local-")


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Streaming SHA-256 of the file's bytes.  Raises OSError to the caller."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(HASH_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def safe_error(exc: BaseException) -> str:
    """``"<ExceptionClass>: <message truncated to 200 chars>"`` - never file content.

    Exception messages can quote a line of the file that failed to parse, so the
    message is also stripped of newlines and any run of 4+ digits is redacted.
    """
    message = " ".join(str(exc).split())
    message = _redact_digits(message)
    if len(message) > MAX_ERROR_CHARS:
        message = message[:MAX_ERROR_CHARS] + "..."
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _redact_digits(text: str) -> str:
    out: list[str] = []
    run = 0
    for char in text:
        if char.isdigit():
            run += 1
            out.append(char)
            continue
        if run >= 4:
            del out[len(out) - run:]
            out.append("X" * run)
        run = 0
        out.append(char)
    if run >= 4:
        del out[len(out) - run:]
        out.append("X" * run)
    return "".join(out)


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


_LOG_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once.  Handlers write to stderr only (no log files)."""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    _LOG_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"agent.{name}")


def short_path(path: str, keep: int = 60) -> str:
    """Shorten a path for log lines.  Paths are not secret; content is."""
    text = str(path)
    return text if len(text) <= keep else "..." + text[-(keep - 3):]


def ensure_parent_dir(path: str | os.PathLike[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
