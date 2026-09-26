"""Local state: atomic claim, retry accounting, crash recovery, idempotent upsert."""

from __future__ import annotations

import threading
from pathlib import Path

from detect_core.contracts import Finding, ScanPayload

from agent.store import (
    FILE_STATUS_DISCOVERED,
    FILE_STATUS_DONE,
    FILE_STATUS_FAILED,
    FILE_STATUS_PROCESSING,
    MAX_ATTEMPTS,
    Store,
)


def make_payload(scan_id: str = "scan_test", roots: list[str] | None = None) -> ScanPayload:
    return ScanPayload(scan_id=scan_id, roots=roots or ["/tmp"], force=False)


def add_files(store: Store, scan_id: str, count: int, status: str = FILE_STATUS_DISCOVERED) -> None:
    store.add_files([
        {
            "scan_id": scan_id, "file_path": f"/tmp/f{i}.txt", "file_hash": f"h{i}",
            "file_type": "txt", "folder_class": "other", "size_bytes": 10,
            "modified_at": "2026-09-26T09:00:00Z", "status": status, "status_reason": None,
        }
        for i in range(count)
    ])


def finding(finding_id: str, tier: str = "confidential", scan_id: str = "scan_test") -> Finding:
    return Finding(
        finding_id=finding_id, device_id="LAPTOP-TEST", scan_id=scan_id,
        file_path="/tmp/f0.txt", file_hash="h0", folder_class="other", file_type="txt",
        location="chunk=0;char=4", finding_kind="item", category="personal",
        pii_type="AADHAAR", doc_type=None, masked_value="XXXXXXXX4821", value_hash="deadbeef",
        holder="individual", masked_in_source=False, confidence=0.93, decided_by="server",
        reason="test", llm_suggested_tier=tier, sensitivity_tier=tier, tier_reason="test",
        policy_version="2026-09-26.1", risk_score=48.0, risk_band="medium",
        detected_at="2026-09-26T09:31:12Z",
    )


def test_schema_applies_and_is_idempotent(tmp_path: Path):
    path = tmp_path / "a.db"
    Store(path).close()
    store = Store(path)
    tables = {
        row["name"]
        for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"kv", "commands", "scans", "files", "findings"} <= tables
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_duplicate_file_rows_are_ignored(store: Store):
    store.create_scan(make_payload(), None)
    add_files(store, "scan_test", 3)
    add_files(store, "scan_test", 3)
    assert store.scan_counts("scan_test")["files_discovered"] == 3


def test_claim_is_atomic_across_threads(store: Store):
    store.create_scan(make_payload(), None)
    add_files(store, "scan_test", 60)

    claimed: list[list[int]] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        local = Store(store.db_path)
        barrier.wait()
        claimed.append([row["id"] for row in local.claim_files(20)])
        local.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    everything = [file_id for batch in claimed for file_id in batch]
    assert len(everything) == 60                       # every file claimed exactly once
    assert len(set(everything)) == 60
    remaining = store.conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE status = ?", (FILE_STATUS_DISCOVERED,)
    ).fetchone()["n"]
    assert remaining == 0


def test_claim_marks_processing_and_counts_attempts(store: Store):
    store.create_scan(make_payload(), None)
    add_files(store, "scan_test", 2)
    rows = store.claim_files(10)
    assert {row["status"] for row in rows} == {FILE_STATUS_PROCESSING}
    assert {row["attempts"] for row in rows} == {1}


def test_release_requeues_then_fails_after_three_attempts(store: Store):
    store.create_scan(make_payload(), None)
    add_files(store, "scan_test", 1)
    for attempt in range(MAX_ATTEMPTS):
        rows = store.claim_files(1)
        assert rows, f"expected a claim on attempt {attempt + 1}"
        requeued, failed = store.release_files([rows[0]["id"]], "ServerError: 503")
        if attempt < MAX_ATTEMPTS - 1:
            assert (requeued, failed) == (1, 0)
        else:
            assert (requeued, failed) == (0, 1)
    assert store.scan_counts("scan_test")["files_failed"] == 1
    assert store.claim_files(1) == []


def test_recover_processing_on_startup(store: Store):
    store.create_scan(make_payload(), None)
    add_files(store, "scan_test", 5)
    store.claim_files(5)
    assert store.recover_processing() == 5
    assert len(store.claim_files(5)) == 5


def test_unchanged_skip_only_after_a_completed_file(store: Store):
    store.create_scan(make_payload(), None)
    add_files(store, "scan_test", 1)
    assert not store.was_done_before("/tmp/f0.txt", "h0")
    rows = store.claim_files(1)
    store.mark_file(rows[0]["id"], FILE_STATUS_DONE, findings_count=2)
    assert store.was_done_before("/tmp/f0.txt", "h0")
    assert not store.was_done_before("/tmp/f0.txt", "different-hash")


def test_save_findings_is_idempotent(store: Store):
    store.create_scan(make_payload(), None)
    assert store.save_findings([finding("fnd_1"), finding("fnd_2")]) == 2
    store.save_findings([finding("fnd_1", tier="restricted")])
    assert store.findings_count() == 2
    assert store.findings_by_tier() == {"restricted": 1, "confidential": 1}


def test_scan_counts_and_completion(store: Store):
    store.create_scan(make_payload(), None)
    add_files(store, "scan_test", 4)
    rows = store.claim_files(4)
    store.mark_file(rows[0]["id"], FILE_STATUS_DONE, findings_count=3)
    store.mark_file(rows[1]["id"], "unscannable", status_reason="encrypted")
    store.mark_file(rows[2]["id"], "skipped", status_reason="unchanged")
    store.mark_file(rows[3]["id"], FILE_STATUS_FAILED)
    counts = store.scan_counts("scan_test")
    assert counts == {
        "files_discovered": 4, "files_done": 1, "files_unscannable": 1,
        "files_skipped": 1, "files_failed": 1, "findings": 3,
    }
    assert not store.scan_is_finished("scan_test")      # crawl_complete not set yet
    store.set_crawl_complete("scan_test")
    assert store.scan_is_finished("scan_test")


def test_scan_queue_is_fifo_and_one_at_a_time(store: Store):
    store.create_scan(make_payload("scan_a"), "cmd_a")
    store.create_scan(make_payload("scan_b"), "cmd_b")
    assert store.next_queued_scan()["scan_id"] == "scan_a"
    store.start_scan("scan_a")
    assert store.has_active_scan()
    assert store.next_queued_scan()["scan_id"] == "scan_b"
    store.finish_scan("scan_a", "completed")
    assert not store.has_active_scan()


def test_files_scanned_total_accumulates(store: Store):
    assert store.files_scanned_total() == 0
    store.bump_files_scanned(5)
    store.bump_files_scanned(7)
    assert store.files_scanned_total() == 12


def test_command_ack_is_stored_verbatim_for_redelivery(store: Store):
    store.record_command("cmd_1", "SCAN", {"scan_id": "scan_1"}, "rejected", "PATH_NOT_FOUND")
    store.record_command("cmd_1", "SCAN", {"scan_id": "scan_1"}, "accepted", None)  # no overwrite
    row = store.get_command("cmd_1")
    assert (row["ack_status"], row["ack_reason"]) == ("rejected", "PATH_NOT_FOUND")


def test_interrupted_crawl_is_requeued_on_startup(store: Store):
    store.create_scan(make_payload("scan_crashed"), "cmd_1")
    store.start_scan("scan_crashed")                    # crashed before crawl_complete
    store.create_scan(make_payload("scan_finished_crawl"), "cmd_2")
    store.start_scan("scan_finished_crawl")
    store.set_crawl_complete("scan_finished_crawl")

    assert store.resume_interrupted_scans() == 1
    assert store.get_scan("scan_crashed")["status"] == "queued"
    assert store.get_scan("scan_finished_crawl")["status"] == "running"
