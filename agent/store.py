"""Local SQLite state: commands, scans, files, findings.

Invariants:
  * **No column ever holds extracted text, OCR text or a raw identifier value.**
    Findings are stored exactly as the server returned them (masked_value +
    value_hash only); chunk text is dropped the moment the HTTP call returns.
  * WAL mode, one connection per thread (``sqlite3`` connections are not safe to
    share), so the Poller, Crawler, two Processors and the Reporter can all work
    at once.
  * Claiming files for processing is atomic under ``BEGIN IMMEDIATE`` so two
    Processor threads never pick the same file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from detect_core.contracts import Finding, ScanPayload

from agent.util import get_logger, iso_now

log = get_logger("store")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

FILE_STATUS_DISCOVERED = "discovered"
FILE_STATUS_PROCESSING = "processing"
FILE_STATUS_DONE = "done"
FILE_STATUS_UNSCANNABLE = "unscannable"
FILE_STATUS_SKIPPED = "skipped"
FILE_STATUS_FAILED = "failed"

SCAN_STATUS_QUEUED = "queued"
SCAN_STATUS_RUNNING = "running"
SCAN_STATUS_COMPLETED = "completed"
SCAN_STATUS_FAILED = "failed"

OPEN_FILE_STATUSES = (FILE_STATUS_DISCOVERED, FILE_STATUS_PROCESSING)
MAX_ATTEMPTS = 3


class Store:
    """Thread-safe facade over the agent's SQLite database."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._apply_schema()

    # ------------------------------------------------------------------ #
    # connections
    # ------------------------------------------------------------------ #

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Serialised immediate transaction (SQLite allows one writer at a time)."""
        with self._write_lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _apply_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ #
    # kv
    # ------------------------------------------------------------------ #

    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def bump_files_scanned(self, delta: int) -> int:
        """Lifetime counter reported in the heartbeat."""
        if delta <= 0:
            return self.files_scanned_total()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO kv(key, value) VALUES('files_scanned_total', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(kv.value AS INTEGER) + ? AS TEXT)",
                (str(delta), delta),
            )
            row = conn.execute("SELECT value FROM kv WHERE key='files_scanned_total'").fetchone()
        return int(row["value"]) if row else 0

    def files_scanned_total(self) -> int:
        raw = self.get_kv("files_scanned_total", "0") or "0"
        try:
            return int(raw)
        except ValueError:
            return 0

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #

    def get_command(self, command_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM commands WHERE command_id = ?", (command_id,)
        ).fetchone()

    def record_command(
        self,
        command_id: str,
        type_: str,
        payload: dict[str, Any],
        ack_status: str,
        ack_reason: Optional[str] = None,
    ) -> None:
        """Store the ack verbatim so a redelivered command replays the same answer."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO commands"
                "(command_id, type, payload_json, ack_status, ack_reason, received_at) "
                "VALUES(?,?,?,?,?,?)",
                (command_id, type_, json.dumps(payload), ack_status, ack_reason, iso_now()),
            )

    # ------------------------------------------------------------------ #
    # scans
    # ------------------------------------------------------------------ #

    def create_scan(
        self,
        payload: ScanPayload,
        command_id: Optional[str],
        status: str = SCAN_STATUS_QUEUED,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO scans"
                "(scan_id, command_id, roots_json, force, include_types, exclude_dirs,"
                " max_file_mb, status, crawl_complete) "
                "VALUES(?,?,?,?,?,?,?,?,0)",
                (
                    payload.scan_id,
                    command_id,
                    json.dumps(payload.roots),
                    int(payload.force),
                    json.dumps(payload.include_types),
                    json.dumps(payload.exclude_dirs),
                    int(payload.max_file_mb),
                    status,
                ),
            )

    def get_scan(self, scan_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()

    def scan_payload(self, scan_id: str) -> Optional[ScanPayload]:
        row = self.get_scan(scan_id)
        if row is None:
            return None
        return ScanPayload(
            scan_id=row["scan_id"],
            roots=json.loads(row["roots_json"]),
            force=bool(row["force"]),
            include_types=json.loads(row["include_types"]),
            exclude_dirs=json.loads(row["exclude_dirs"]),
            max_file_mb=int(row["max_file_mb"]),
        )

    def next_queued_scan(self) -> Optional[sqlite3.Row]:
        """Oldest queued scan.  Scans run one at a time, FIFO (api.md 3.3)."""
        return self.conn.execute(
            "SELECT * FROM scans WHERE status = ? ORDER BY rowid LIMIT 1", (SCAN_STATUS_QUEUED,)
        ).fetchone()

    def running_scans(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM scans WHERE status = ? ORDER BY rowid", (SCAN_STATUS_RUNNING,)
            ).fetchall()
        )

    def has_active_scan(self) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM scans WHERE status = ? LIMIT 1", (SCAN_STATUS_RUNNING,)
        ).fetchone()
        return row is not None

    def start_scan(self, scan_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE scans SET status = ?, started_at = COALESCE(started_at, ?) WHERE scan_id = ?",
                (SCAN_STATUS_RUNNING, iso_now(), scan_id),
            )

    def set_crawl_complete(self, scan_id: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE scans SET crawl_complete = 1 WHERE scan_id = ?", (scan_id,))

    def finish_scan(self, scan_id: str, status: str, error: Optional[str] = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE scans SET status = ?, error = ?, finished_at = ? WHERE scan_id = ?",
                (status, error, iso_now(), scan_id),
            )

    def scan_counts(self, scan_id: str) -> dict[str, int]:
        """Cumulative counts for ScanProgress / ScanComplete, derived from `files`."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(findings_count), 0) AS f "
            "FROM files WHERE scan_id = ? GROUP BY status",
            (scan_id,),
        ).fetchall()
        by_status = {row["status"]: (row["n"], row["f"]) for row in rows}
        discovered_total = sum(n for n, _ in by_status.values())
        return {
            "files_discovered": discovered_total,
            "files_done": by_status.get(FILE_STATUS_DONE, (0, 0))[0],
            "files_unscannable": by_status.get(FILE_STATUS_UNSCANNABLE, (0, 0))[0],
            "files_skipped": by_status.get(FILE_STATUS_SKIPPED, (0, 0))[0],
            "files_failed": by_status.get(FILE_STATUS_FAILED, (0, 0))[0],
            "findings": sum(f for _, f in by_status.values()),
        }

    def open_file_count(self, scan_id: str) -> int:
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM files WHERE scan_id = ? AND status IN "
            f"({','.join('?' * len(OPEN_FILE_STATUSES))})",
            (scan_id, *OPEN_FILE_STATUSES),
        ).fetchone()
        return int(row["n"])

    def scan_is_finished(self, scan_id: str) -> bool:
        row = self.get_scan(scan_id)
        if row is None or not row["crawl_complete"]:
            return False
        return self.open_file_count(scan_id) == 0

    # ------------------------------------------------------------------ #
    # files
    # ------------------------------------------------------------------ #

    def add_files(self, rows: Sequence[dict[str, Any]]) -> int:
        """Insert discovered/skipped/unscannable files.  Ignores duplicates."""
        if not rows:
            return 0
        now = iso_now()
        params = [
            (
                row["scan_id"], row["file_path"], row.get("file_hash"), row["file_type"],
                row["folder_class"], int(row["size_bytes"]), row["modified_at"],
                row["status"], row.get("status_reason"), now,
            )
            for row in rows
        ]
        with self._tx() as conn:
            cursor = conn.executemany(
                "INSERT OR IGNORE INTO files"
                "(scan_id, file_path, file_hash, file_type, folder_class, size_bytes,"
                " modified_at, status, status_reason, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                params,
            )
            return cursor.rowcount or 0

    def was_done_before(self, file_path: str, file_hash: str) -> bool:
        """Unchanged-skip: same path + hash already completed in an earlier scan."""
        row = self.conn.execute(
            "SELECT 1 FROM files WHERE file_path = ? AND file_hash = ? AND status IN (?, ?) LIMIT 1",
            (file_path, file_hash, FILE_STATUS_DONE, FILE_STATUS_UNSCANNABLE),
        ).fetchone()
        return row is not None

    def claim_files(self, limit: int, scan_id: Optional[str] = None) -> list[sqlite3.Row]:
        """Atomically move up to ``limit`` discovered files to ``processing``.

        Two Processor threads calling this concurrently get disjoint sets.
        """
        with self._tx() as conn:
            if scan_id:
                select = (
                    "SELECT id FROM files WHERE status = ? AND scan_id = ? ORDER BY id LIMIT ?"
                )
                args: tuple[Any, ...] = (FILE_STATUS_DISCOVERED, scan_id, limit)
            else:
                select = "SELECT id FROM files WHERE status = ? ORDER BY id LIMIT ?"
                args = (FILE_STATUS_DISCOVERED, limit)
            ids = [row["id"] for row in conn.execute(select, args).fetchall()]
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE files SET status = ?, attempts = attempts + 1, updated_at = ? "
                f"WHERE id IN ({placeholders})",
                (FILE_STATUS_PROCESSING, iso_now(), *ids),
            )
            return list(
                conn.execute(
                    f"SELECT * FROM files WHERE id IN ({placeholders}) ORDER BY id", ids
                ).fetchall()
            )

    def mark_file(
        self,
        file_id: int,
        status: str,
        status_reason: Optional[str] = None,
        findings_count: Optional[int] = None,
        last_error: Optional[str] = None,
        file_hash: Optional[str] = None,
    ) -> None:
        sets = ["status = ?", "updated_at = ?"]
        args: list[Any] = [status, iso_now()]
        if status_reason is not None:
            sets.append("status_reason = ?")
            args.append(status_reason)
        if findings_count is not None:
            sets.append("findings_count = ?")
            args.append(int(findings_count))
        if last_error is not None:
            sets.append("last_error = ?")
            args.append(last_error)
        if file_hash is not None:
            sets.append("file_hash = ?")
            args.append(file_hash)
        args.append(file_id)
        with self._tx() as conn:
            conn.execute(f"UPDATE files SET {', '.join(sets)} WHERE id = ?", args)

    def release_files(self, file_ids: Iterable[int], error: str) -> tuple[int, int]:
        """Return files to ``discovered`` after a retryable error.

        A file that has already been attempted ``MAX_ATTEMPTS`` times goes to
        ``failed`` instead.  Returns (requeued, failed).
        """
        ids = list(file_ids)
        if not ids:
            return (0, 0)
        placeholders = ",".join("?" * len(ids))
        now = iso_now()
        with self._tx() as conn:
            failed = conn.execute(
                f"UPDATE files SET status = ?, last_error = ?, updated_at = ? "
                f"WHERE id IN ({placeholders}) AND attempts >= ?",
                (FILE_STATUS_FAILED, error, now, *ids, MAX_ATTEMPTS),
            ).rowcount or 0
            requeued = conn.execute(
                f"UPDATE files SET status = ?, last_error = ?, updated_at = ? "
                f"WHERE id IN ({placeholders}) AND status = ?",
                (FILE_STATUS_DISCOVERED, error, now, *ids, FILE_STATUS_PROCESSING),
            ).rowcount or 0
        return (requeued, failed)

    def recover_processing(self) -> int:
        """Startup recovery: a crash leaves ``processing`` rows behind (api.md 8)."""
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE files SET status = ?, updated_at = ? WHERE status = ?",
                (FILE_STATUS_DISCOVERED, iso_now(), FILE_STATUS_PROCESSING),
            )
        count = cursor.rowcount or 0
        if count:
            log.info("recovered %d files from processing -> discovered", count)
        return count

    # ------------------------------------------------------------------ #
    # findings
    # ------------------------------------------------------------------ #

    def save_findings(self, findings: Sequence[Finding]) -> int:
        """Upsert by finding_id (resending a batch must be idempotent).

        Only metadata is written; ``Finding`` has no raw value by construction.
        """
        if not findings:
            return 0
        params = [
            (
                f.finding_id, f.scan_id, f.file_path, f.file_hash, f.location, f.finding_kind,
                f.category, f.pii_type, f.doc_type, f.masked_value, f.value_hash, f.holder,
                float(f.confidence), f.decided_by, f.reason, f.sensitivity_tier, f.tier_reason,
                float(f.risk_score), f.risk_band, f.detected_at,
            )
            for f in findings
        ]
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO findings"
                "(finding_id, scan_id, file_path, file_hash, location, finding_kind, category,"
                " pii_type, doc_type, masked_value, value_hash, holder, confidence, decided_by,"
                " reason, sensitivity_tier, tier_reason, risk_score, risk_band, detected_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(finding_id) DO UPDATE SET "
                " scan_id=excluded.scan_id, location=excluded.location,"
                " masked_value=excluded.masked_value, value_hash=excluded.value_hash,"
                " confidence=excluded.confidence, decided_by=excluded.decided_by,"
                " reason=excluded.reason, sensitivity_tier=excluded.sensitivity_tier,"
                " tier_reason=excluded.tier_reason, risk_score=excluded.risk_score,"
                " risk_band=excluded.risk_band, detected_at=excluded.detected_at",
                params,
            )
        return len(params)

    def findings_by_tier(self, scan_id: Optional[str] = None) -> dict[str, int]:
        if scan_id:
            rows = self.conn.execute(
                "SELECT sensitivity_tier AS t, COUNT(*) AS n FROM findings WHERE scan_id = ? GROUP BY t",
                (scan_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT sensitivity_tier AS t, COUNT(*) AS n FROM findings GROUP BY t"
            ).fetchall()
        return {row["t"]: int(row["n"]) for row in rows}

    def findings_count(self, scan_id: Optional[str] = None) -> int:
        if scan_id:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM findings WHERE scan_id = ?", (scan_id,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM findings").fetchone()
        return int(row["n"])
