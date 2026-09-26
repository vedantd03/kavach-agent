"""Walk the scan roots and record every candidate file (api.md 8).

The crawler does no parsing: it decides *what* to look at, hashes the bytes, and
writes one ``files`` row per candidate.  Oversize and unreadable files keep their
reason and stay ``discovered``, so the Processor still sends them to ``/detect``
as a ``FileMeta`` with no chunks (api.md 6) before marking them unscannable;
unchanged files (same path + hash already completed) are ``skipped`` unless
``force``.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Iterator, Optional

from detect_core.contracts import ScanPayload, classify_folder, file_type_for

from agent.store import FILE_STATUS_DISCOVERED, FILE_STATUS_SKIPPED, Store
from agent.util import get_logger, iso_from_mtime, iso_now, safe_error, sha256_file

log = get_logger("crawler")

INSERT_BATCH = 200


@dataclass
class CrawlStats:
    discovered: int = 0
    skipped: int = 0
    unscannable: int = 0
    ignored: int = 0            # wrong extension / excluded directory


@dataclass
class _Candidate:
    file_path: str
    size_bytes: int
    modified_at: str
    file_type: str


def crawl_scan(
    store: Store,
    payload: ScanPayload,
    stop: Optional[threading.Event] = None,
) -> CrawlStats:
    """Discover every file under ``payload.roots`` and write the ``files`` rows."""
    stats = CrawlStats()
    include = {t.lower().lstrip(".") for t in payload.include_types}
    exclude = {d.lower() for d in payload.exclude_dirs}
    max_bytes = max(0, payload.max_file_mb) * 1024 * 1024
    pending: list[dict[str, object]] = []

    for candidate in _walk(payload.roots, include, exclude, stats, stop):
        if stop is not None and stop.is_set():
            break
        row = _row_for(store, payload, candidate, max_bytes, stats)
        pending.append(row)
        if len(pending) >= INSERT_BATCH:
            store.add_files(pending)
            pending.clear()

    if pending:
        store.add_files(pending)

    log.info(
        "crawl %s: %d discovered, %d skipped, %d unreadable, %d ignored",
        payload.scan_id, stats.discovered, stats.skipped, stats.unscannable, stats.ignored,
    )
    return stats


def _row_for(
    store: Store,
    payload: ScanPayload,
    candidate: _Candidate,
    max_bytes: int,
    stats: CrawlStats,
) -> dict[str, object]:
    base: dict[str, object] = {
        "scan_id": payload.scan_id,
        "file_path": candidate.file_path,
        "file_type": candidate.file_type,
        "folder_class": classify_folder(candidate.file_path),
        "size_bytes": candidate.size_bytes,
        "modified_at": candidate.modified_at,
        "file_hash": "",
    }

    if max_bytes and candidate.size_bytes > max_bytes:
        stats.unscannable += 1
        return {**base, "status": FILE_STATUS_DISCOVERED, "status_reason": "oversize"}

    try:
        file_hash = sha256_file(candidate.file_path)
    except PermissionError:
        stats.unscannable += 1
        return {**base, "status": FILE_STATUS_DISCOVERED, "status_reason": "permission_denied"}
    except OSError as exc:
        log.info("hash failed: %s", safe_error(exc))
        stats.unscannable += 1
        return {**base, "status": FILE_STATUS_DISCOVERED, "status_reason": "corrupt"}

    base["file_hash"] = file_hash

    if not payload.force and store.was_done_before(candidate.file_path, file_hash):
        stats.skipped += 1
        return {**base, "status": FILE_STATUS_SKIPPED, "status_reason": "unchanged"}

    stats.discovered += 1
    return {**base, "status": FILE_STATUS_DISCOVERED, "status_reason": None}


def _walk(
    roots: list[str],
    include: set[str],
    exclude: set[str],
    stats: CrawlStats,
    stop: Optional[threading.Event],
) -> Iterator[_Candidate]:
    seen_dirs: set[str] = set()

    for root in roots:
        if stop is not None and stop.is_set():
            return
        root_path = os.path.abspath(os.path.expanduser(root))
        if not os.path.exists(root_path):
            log.warning("root does not exist, skipping: %s", root_path)
            continue
        if os.path.isfile(root_path):
            candidate = _candidate_for(root_path, include, stats)
            if candidate is not None:
                yield candidate
            continue

        for dirpath, dirnames, filenames in os.walk(root_path, topdown=True, followlinks=False):
            if stop is not None and stop.is_set():
                return

            # Prune excluded, symlinked and already-visited directories in place.
            kept: list[str] = []
            for name in dirnames:
                full = os.path.join(dirpath, name)
                if name.lower() in exclude:
                    stats.ignored += 1
                    continue
                if os.path.islink(full):
                    continue
                real = _real(full)
                if real in seen_dirs:
                    continue
                seen_dirs.add(real)
                kept.append(name)
            dirnames[:] = kept

            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                candidate = _candidate_for(full, include, stats)
                if candidate is not None:
                    yield candidate


def _candidate_for(path: str, include: set[str], stats: CrawlStats) -> Optional[_Candidate]:
    file_type = file_type_for(path)
    if file_type not in include:
        stats.ignored += 1
        return None
    try:
        info = os.stat(path)
    except OSError as exc:
        log.info("stat failed: %s", safe_error(exc))
        stats.ignored += 1
        return None
    return _Candidate(
        file_path=os.path.abspath(path),
        size_bytes=int(info.st_size),
        modified_at=iso_from_mtime(info.st_mtime),
        file_type=file_type,
    )


def _real(path: str) -> str:
    try:
        return os.path.realpath(path).lower()
    except OSError:
        return path.lower()


def existing_roots(roots: list[str]) -> tuple[list[str], list[str]]:
    """Split roots into (existing, missing).  Used by the Poller before acking."""
    present: list[str] = []
    missing: list[str] = []
    for root in roots:
        expanded = os.path.abspath(os.path.expanduser(root))
        (present if os.path.exists(expanded) else missing).append(root)
    return present, missing


def run_crawl(store: Store, scan_id: str, stop: Optional[threading.Event] = None) -> CrawlStats:
    """Crawl a scan that is already in the local database, then flag crawl_complete."""
    payload = store.scan_payload(scan_id)
    if payload is None:
        raise ValueError(f"unknown scan {scan_id}")
    store.start_scan(scan_id)
    started = iso_now()
    try:
        stats = crawl_scan(store, payload, stop)
    except Exception as exc:                                     # noqa: BLE001
        log.error("crawl failed for %s: %s", scan_id, safe_error(exc))
        store.finish_scan(scan_id, "failed", safe_error(exc))
        raise
    store.set_crawl_complete(scan_id)
    log.debug("crawl of %s started %s finished %s", scan_id, started, iso_now())
    return stats
