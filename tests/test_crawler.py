"""Discovery rules: include types, excluded directories, size, unchanged skip."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from detect_core.contracts import ScanPayload

from agent.crawler import crawl_scan, existing_roots, run_crawl
from agent.store import FILE_STATUS_DONE, Store


def payload_for(root: Path, **overrides) -> ScanPayload:
    base = dict(scan_id="scan_test", roots=[str(root)], force=False, max_file_mb=50)
    base.update(overrides)
    return ScanPayload(**base)


def files_in(store: Store) -> dict[str, tuple[str, str | None]]:
    return {
        Path(row["file_path"]).name: (row["status"], row["status_reason"])
        for row in store.conn.execute("SELECT * FROM files")
    }


def test_crawl_filters_types_and_excluded_dirs(store: Store, corpus: Path):
    payload = payload_for(corpus)
    store.create_scan(payload, None)
    stats = crawl_scan(store, payload)

    found = files_in(store)
    assert set(found) == {"chat.txt", "notes.md", "customers.csv"}
    assert "skip.txt" not in found and "config.txt" not in found   # node_modules, .git
    assert "ignored.bin" not in found                              # not in include_types
    assert stats.discovered == 3


def test_include_types_can_be_narrowed(store: Store, corpus: Path):
    payload = payload_for(corpus, include_types=["csv"])
    store.create_scan(payload, None)
    crawl_scan(store, payload)
    assert set(files_in(store)) == {"customers.csv"}


def test_folder_class_and_hash_are_recorded(store: Store, corpus: Path):
    payload = payload_for(corpus)
    store.create_scan(payload, None)
    crawl_scan(store, payload)
    rows = {Path(row["file_path"]).name: row for row in store.conn.execute("SELECT * FROM files")}
    assert rows["chat.txt"]["folder_class"] == "documents"
    assert rows["customers.csv"]["folder_class"] == "downloads"
    assert len(rows["chat.txt"]["file_hash"]) == 64
    assert rows["chat.txt"]["size_bytes"] > 0
    assert rows["chat.txt"]["modified_at"].endswith("Z")


def test_oversize_file_is_unscannable_without_being_read(store: Store, tmp_path: Path):
    big = tmp_path / "big.log"
    big.write_text("x" * (2 * 1024 * 1024), encoding="utf-8")
    payload = payload_for(tmp_path, max_file_mb=1)
    store.create_scan(payload, None)
    crawl_scan(store, payload)
    assert files_in(store)["big.log"] == ("unscannable", "oversize")
    row = store.conn.execute("SELECT file_hash FROM files").fetchone()
    assert row["file_hash"] == ""                       # not hashed, not opened


def test_unchanged_files_are_skipped_unless_forced(store: Store, corpus: Path):
    first = payload_for(corpus, scan_id="scan_1")
    store.create_scan(first, None)
    crawl_scan(store, first)
    for row in store.claim_files(10):
        store.mark_file(row["id"], FILE_STATUS_DONE, findings_count=0)

    second = payload_for(corpus, scan_id="scan_2")
    store.create_scan(second, None)
    stats = crawl_scan(store, second)
    assert stats.skipped == 3 and stats.discovered == 0
    assert store.scan_counts("scan_2")["files_skipped"] == 3

    third = payload_for(corpus, scan_id="scan_3", force=True)
    store.create_scan(third, None)
    stats = crawl_scan(store, third)
    assert stats.discovered == 3 and stats.skipped == 0


def test_changed_file_is_rediscovered(store: Store, corpus: Path):
    first = payload_for(corpus, scan_id="scan_1")
    store.create_scan(first, None)
    crawl_scan(store, first)
    for row in store.claim_files(10):
        store.mark_file(row["id"], FILE_STATUS_DONE)

    (corpus / "Documents" / "chat.txt").write_text("now with different content", encoding="utf-8")
    second = payload_for(corpus, scan_id="scan_2")
    store.create_scan(second, None)
    stats = crawl_scan(store, second)
    assert stats.discovered == 1 and stats.skipped == 2


def test_a_file_can_be_a_root(store: Store, corpus: Path):
    target = corpus / "Documents" / "chat.txt"
    payload = payload_for(corpus, roots=[str(target)])
    store.create_scan(payload, None)
    crawl_scan(store, payload)
    assert set(files_in(store)) == {"chat.txt"}


def test_missing_root_is_reported_not_fatal(store: Store, corpus: Path, tmp_path: Path):
    payload = payload_for(corpus, roots=[str(corpus), str(tmp_path / "nope")])
    store.create_scan(payload, None)
    stats = crawl_scan(store, payload)
    assert stats.discovered == 3


def test_existing_roots_splits_present_and_missing(corpus: Path, tmp_path: Path):
    present, missing = existing_roots([str(corpus), str(tmp_path / "gone")])
    assert present == [str(corpus)]
    assert missing == [str(tmp_path / "gone")]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlinked_directories_are_not_followed(store: Store, corpus: Path, tmp_path: Path):
    link = corpus / "Documents" / "loop"
    link.symlink_to(corpus, target_is_directory=True)
    payload = payload_for(corpus)
    store.create_scan(payload, None)
    stats = crawl_scan(store, payload)
    assert stats.discovered == 3


def test_run_crawl_marks_running_then_crawl_complete(store: Store, corpus: Path):
    payload = payload_for(corpus)
    store.create_scan(payload, None)
    run_crawl(store, "scan_test")
    row = store.get_scan("scan_test")
    assert row["status"] == "running"
    assert row["crawl_complete"] == 1
    assert row["started_at"] is not None


def test_max_file_mb_zero_disables_the_size_limit(store: Store, tmp_path: Path):
    (tmp_path / "big.log").write_text("x" * 4096, encoding="utf-8")
    payload = payload_for(tmp_path, scan_id="scan_nolimit", max_file_mb=0)
    store.create_scan(payload, None)
    crawl_scan(store, payload)
    assert files_in(store)["big.log"][0] == "discovered"
