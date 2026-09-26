"""Claim discovered files, extract their text, and send it to the server.

This is the only component that holds file text in memory, and it holds it for
exactly as long as one ``POST /detect`` call: the chunks are dropped as soon as
the response is parsed, and only finding metadata is written to SQLite.

Batching follows api.md 3.5: <= 20 files, <= 2,000 chunks, <= 8 MB per request,
and a file is never split across requests.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional, Sequence

from detect_core.contracts import (
    MAX_BATCH_BYTES,
    MAX_CHUNKS_PER_BATCH,
    MAX_FILES_PER_BATCH,
    Chunk,
    DetectRequest,
    FileMeta,
    OcrResult,
)

from agent.client import ServerClient, ServerError
from agent.extractors import FileFacts, extract_file
from agent.store import (
    FILE_STATUS_DONE,
    FILE_STATUS_FAILED,
    FILE_STATUS_UNSCANNABLE,
    Store,
)
from agent.util import get_logger, iso_now, safe_error, short_path

log = get_logger("processor")

#: Rough per-chunk JSON overhead (ids, paths, nulls) when sizing a request.
_CHUNK_OVERHEAD_BYTES = 400


@dataclass
class ProcessStats:
    files_done: int = 0
    files_unscannable: int = 0
    files_failed: int = 0
    files_requeued: int = 0
    chunks_sent: int = 0
    findings: int = 0
    ocr_pages: int = 0
    pending: int = 0
    batches: int = 0


@dataclass
class _Prepared:
    """One extracted file, ready to go into a request."""

    file_id: int
    meta: FileMeta
    chunks: list[Chunk] = field(default_factory=list)
    bytes_estimate: int = 0


class Processor:
    """Runs one claim-extract-send cycle at a time; safe to run in two threads."""

    def __init__(
        self,
        store: Store,
        client: ServerClient,
        device_id: str,
        *,
        send_local_scan_id: bool = True,
        stop: Optional[threading.Event] = None,
    ) -> None:
        self.store = store
        self.client = client
        self.device_id = device_id
        self.send_local_scan_id = send_local_scan_id
        self.stop = stop or threading.Event()

    # ------------------------------------------------------------------ #

    def process_once(self, scan_id: Optional[str] = None) -> Optional[ProcessStats]:
        """Claim one batch of files and process it.  ``None`` if nothing to do."""
        rows = self.store.claim_files(MAX_FILES_PER_BATCH, scan_id)
        if not rows:
            return None

        stats = ProcessStats()
        prepared: list[_Prepared] = []
        for row in rows:
            if self.stop.is_set():
                self.store.release_files([row["id"]], "stopped")
                stats.files_requeued += 1
                continue
            prepared.append(self._prepare(row, stats))

        for batch in self._batches(prepared):
            if self.stop.is_set():
                self.store.release_files([item.file_id for item in batch], "stopped")
                stats.files_requeued += len(batch)
                continue
            self._send(batch, rows[0]["scan_id"], stats)
        return stats

    def run_until_idle(self, scan_id: Optional[str] = None) -> ProcessStats:
        """Used by the CLI: keep processing until no discovered files remain."""
        total = ProcessStats()
        while not self.stop.is_set():
            stats = self.process_once(scan_id)
            if stats is None:
                break
            _accumulate(total, stats)
        return total

    # ------------------------------------------------------------------ #
    # extraction
    # ------------------------------------------------------------------ #

    def _prepare(self, row, stats: ProcessStats) -> _Prepared:                # type: ignore[no-untyped-def]
        facts = FileFacts(
            file_path=row["file_path"],
            file_hash=row["file_hash"] or "",
            file_type=row["file_type"],
            folder_class=row["folder_class"],
        )
        meta = FileMeta(
            file_path=row["file_path"],
            file_hash=facts.file_hash,
            file_type=row["file_type"],
            folder_class=row["folder_class"],
            size_bytes=int(row["size_bytes"]),
            modified_at=row["modified_at"],
            status="ok",
            status_reason=None,
        )

        # A file the crawler already judged unreadable goes to /detect as
        # metadata with no chunks, so the server records it too.
        if row["status_reason"] in {"oversize", "permission_denied"} and not facts.file_hash:
            meta.status = "unscannable"
            meta.status_reason = row["status_reason"]
            return _Prepared(int(row["id"]), meta)

        result = extract_file(row["file_path"], facts, ocr=self._ocr)
        stats.ocr_pages += result.ocr_pages
        meta.status = "unscannable" if result.status == "unscannable" else "ok"
        meta.status_reason = result.status_reason

        chunks = result.chunks if meta.status == "ok" else []
        size = sum(len(chunk.text.encode("utf-8")) + _CHUNK_OVERHEAD_BYTES for chunk in chunks)
        return _Prepared(int(row["id"]), meta, chunks, size)

    def _ocr(
        self,
        image_bytes: bytes,
        *,
        file_hash: str,
        page: Optional[int],
        filename: str,
        content_type: str,
    ) -> Optional[OcrResult]:
        """Server-side OCR.  Returns None on failure so extraction can continue."""
        try:
            return self.client.ocr(
                image_bytes,
                file_hash=file_hash,
                page=page,
                filename=filename,
                content_type=content_type,
            )
        except ServerError as exc:
            log.warning("ocr failed (page %s): %s", page, exc)
            return None

    # ------------------------------------------------------------------ #
    # batching
    # ------------------------------------------------------------------ #

    def _batches(self, prepared: Sequence[_Prepared]) -> list[list[_Prepared]]:
        batches: list[list[_Prepared]] = []
        current: list[_Prepared] = []
        chunk_count = 0
        byte_count = 0

        for item in prepared:
            item = self._fit_single(item)
            over = (
                len(current) >= MAX_FILES_PER_BATCH
                or chunk_count + len(item.chunks) > MAX_CHUNKS_PER_BATCH
                or byte_count + item.bytes_estimate > MAX_BATCH_BYTES
            )
            if current and over:
                batches.append(current)
                current, chunk_count, byte_count = [], 0, 0
            current.append(item)
            chunk_count += len(item.chunks)
            byte_count += item.bytes_estimate
        if current:
            batches.append(current)
        return batches

    def _fit_single(self, item: _Prepared) -> _Prepared:
        """A single file must fit one request; drop the tail if it cannot."""
        if item.bytes_estimate <= MAX_BATCH_BYTES and len(item.chunks) <= MAX_CHUNKS_PER_BATCH:
            return item
        kept: list[Chunk] = []
        total = 0
        for chunk in item.chunks:
            size = len(chunk.text.encode("utf-8")) + _CHUNK_OVERHEAD_BYTES
            if total + size > MAX_BATCH_BYTES or len(kept) >= MAX_CHUNKS_PER_BATCH:
                break
            kept.append(chunk)
            total += size
        log.warning("file too large for one request, sending %d/%d chunks: %s",
                    len(kept), len(item.chunks), short_path(item.meta.file_path))
        item.chunks = kept
        item.bytes_estimate = total
        item.meta.status_reason = item.meta.status_reason or "truncated"
        return item

    # ------------------------------------------------------------------ #
    # send
    # ------------------------------------------------------------------ #

    def _send(self, batch: list[_Prepared], scan_id: Optional[str], stats: ProcessStats) -> None:
        if not batch:
            return
        request = DetectRequest(
            device_id=self.device_id,
            scan_id=self._scan_id_for_wire(scan_id),
            files=[item.meta for item in batch],
            chunks=[chunk for item in batch for chunk in item.chunks],
        )
        try:
            response = self.client.detect(request)
        except ServerError as exc:
            if exc.status_code == 413 and len(batch) > 1:
                middle = len(batch) // 2
                log.warning("batch too large, splitting %d files", len(batch))
                self._send(batch[:middle], scan_id, stats)
                self._send(batch[middle:], scan_id, stats)
                return
            if exc.retryable or exc.status_code is None:
                requeued, failed = self.store.release_files(
                    [item.file_id for item in batch], safe_error(exc)
                )
                stats.files_requeued += requeued
                stats.files_failed += failed
                log.warning("detect failed, %d requeued / %d failed: %s", requeued, failed, exc)
                return
            for item in batch:                                   # permanent rejection
                self.store.mark_file(
                    item.file_id, FILE_STATUS_FAILED, last_error=safe_error(exc)
                )
            stats.files_failed += len(batch)
            log.error("detect rejected %d files: %s", len(batch), exc)
            return

        stats.batches += 1
        stats.chunks_sent += len(request.chunks)
        stats.pending += int(response.pending or 0)

        saved = self.store.save_findings(response.findings)
        stats.findings += saved

        per_file: dict[str, int] = {}
        for finding in response.findings:
            per_file[finding.file_path] = per_file.get(finding.file_path, 0) + 1

        scanned_ok = 0
        for item in batch:
            count = per_file.get(item.meta.file_path, 0)
            if item.meta.status == "unscannable":
                self.store.mark_file(
                    item.file_id, FILE_STATUS_UNSCANNABLE,
                    status_reason=item.meta.status_reason, findings_count=count,
                )
                stats.files_unscannable += 1
            else:
                self.store.mark_file(
                    item.file_id, FILE_STATUS_DONE,
                    status_reason=item.meta.status_reason, findings_count=count,
                )
                stats.files_done += 1
                scanned_ok += 1

        if scanned_ok:
            self.store.bump_files_scanned(scanned_ok)
        log.info(
            "detect ok: %d files, %d chunks, %d findings, pending=%d (%s)",
            len(batch), len(request.chunks), saved, response.pending, iso_now(),
        )

    def _scan_id_for_wire(self, scan_id: Optional[str]) -> Optional[str]:
        if scan_id and scan_id.startswith("local-") and not self.send_local_scan_id:
            return None
        return scan_id


def _accumulate(total: ProcessStats, part: ProcessStats) -> None:
    total.files_done += part.files_done
    total.files_unscannable += part.files_unscannable
    total.files_failed += part.files_failed
    total.files_requeued += part.files_requeued
    total.chunks_sent += part.chunks_sent
    total.findings += part.findings
    total.ocr_pages += part.ocr_pages
    total.pending += part.pending
    total.batches += part.batches
