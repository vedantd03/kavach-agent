"""End-to-end against tools/mock_server.py: poll, ack, crawl, detect, report."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from detect_core.contracts import MAX_FILES_PER_BATCH, CreateScanRequest

from agent.client import ServerClient
from agent.config import Config
from agent.crawler import run_crawl
from agent.main import Poller, Reporter
from agent.processor import Processor
from agent.store import Store


def create_server_scan(client: ServerClient, roots: list[str], device_id: str = "LAPTOP-TEST") -> str:
    """Admin-side: queue a scan for a device that has already polled."""
    request = CreateScanRequest(device_id=device_id, roots=roots, force=False)
    response = client._client.post("/admin/scans", json=request.model_dump(mode="json"))
    assert response.status_code == 201, response.text
    return response.json()["scan_id"]


@pytest.fixture()
def poller(store: Store, client: ServerClient, config: Config) -> Poller:
    return Poller(store, client, config, threading.Event())


# --------------------------------------------------------------------------- #
# poller
# --------------------------------------------------------------------------- #

def test_poll_with_nothing_pending(poller: Poller):
    assert poller.poll_once() is None


def test_poll_accepts_a_scan_and_queues_it(poller: Poller, store: Store, client: ServerClient, corpus: Path):
    poller.poll_once()                                   # registers the device
    scan_id = create_server_scan(client, [str(corpus)])

    assert poller.poll_once() == scan_id
    row = store.get_scan(scan_id)
    assert row["status"] == "queued" and row["command_id"]

    view = client._client.get(f"/admin/scans/{scan_id}").json()
    assert view["status"] == "running"                    # accepted ack moved it on


def test_redelivered_command_is_acked_again_not_rerun(poller: Poller, store: Store, client: ServerClient, corpus: Path, monkeypatch):
    poller.poll_once()
    scan_id = create_server_scan(client, [str(corpus)])
    assert poller.poll_once() == scan_id

    # force the server to redeliver the same command
    monkeypatch.setenv("COMMAND_REDELIVERY_SEC", "0")
    assert poller.poll_once() is None                     # deduped
    assert store.conn.execute("SELECT COUNT(*) AS n FROM scans").fetchone()["n"] == 1


def test_missing_root_is_rejected_with_path_not_found(poller: Poller, store: Store, client: ServerClient, tmp_path: Path):
    poller.poll_once()
    scan_id = create_server_scan(client, [str(tmp_path / "nowhere")])
    assert poller.poll_once() is None

    assert store.get_scan(scan_id) is None
    view = client._client.get(f"/admin/scans/{scan_id}").json()
    assert view["status"] == "rejected" and view["reject_reason"] == "PATH_NOT_FOUND"


def test_partial_roots_are_accepted(poller: Poller, store: Store, client: ServerClient, corpus: Path, tmp_path: Path):
    poller.poll_once()
    scan_id = create_server_scan(client, [str(corpus), str(tmp_path / "nowhere")])
    assert poller.poll_once() == scan_id
    payload = store.scan_payload(scan_id)
    assert payload is not None and payload.roots == [str(corpus)]


def test_unsupported_command_is_rejected(poller: Poller, store: Store, client: ServerClient, corpus: Path, mock_app):
    import mock_server

    poller.poll_once()
    create_server_scan(client, [str(corpus)])
    for command in mock_server.STATE.commands.values():
        command["type"] = "RUN_ACTIONS"

    assert poller.poll_once() is None
    row = store.get_command(next(iter(mock_server.STATE.commands)))
    assert (row["ack_status"], row["ack_reason"]) == ("rejected", "UNSUPPORTED_COMMAND")


# --------------------------------------------------------------------------- #
# processing
# --------------------------------------------------------------------------- #

def test_full_local_scan_stores_findings(store: Store, client: ServerClient, config: Config, corpus: Path):
    from detect_core.contracts import ScanPayload

    payload = ScanPayload(scan_id="local-test", roots=[str(corpus)], force=False)
    store.create_scan(payload, None)
    run_crawl(store, "local-test")

    processor = Processor(store, client, config.device_id)
    stats = processor.run_until_idle("local-test")

    assert stats.files_done == 3
    assert stats.findings == 3                            # mock emits one per file
    counts = store.scan_counts("local-test")
    assert counts["files_done"] == 3 and counts["findings"] == 3
    assert store.findings_by_tier("local-test") == {"confidential": 3}
    assert store.files_scanned_total() == 3

    # findings arrived on the server too, tagged with the scan
    findings = client._client.get("/admin/findings", params={"scan_id": "local-test"}).json()
    assert len(findings) == 3


def test_detect_batches_respect_the_file_limit(store: Store, client: ServerClient, config: Config, tmp_path: Path):
    from detect_core.contracts import ScanPayload

    root = tmp_path / "many"
    root.mkdir()
    for index in range(MAX_FILES_PER_BATCH + 5):
        (root / f"f{index}.txt").write_text(f"file number {index}\n", encoding="utf-8")

    payload = ScanPayload(scan_id="local-many", roots=[str(root)], force=False)
    store.create_scan(payload, None)
    run_crawl(store, "local-many")

    stats = Processor(store, client, config.device_id).run_until_idle("local-many")
    assert stats.files_done == MAX_FILES_PER_BATCH + 5
    assert stats.batches == 2                             # 20 + 5


def test_unscannable_file_still_reaches_the_server(store: Store, client: ServerClient, config: Config, tmp_path: Path):
    from detect_core.contracts import ScanPayload

    root = tmp_path / "locked"
    root.mkdir()
    (root / "locked.docx").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    (root / "fine.txt").write_text("ordinary text here", encoding="utf-8")

    payload = ScanPayload(scan_id="local-unscannable", roots=[str(root)], force=False)
    store.create_scan(payload, None)
    run_crawl(store, "local-unscannable")
    stats = Processor(store, client, config.device_id).run_until_idle("local-unscannable")

    assert stats.files_unscannable == 1 and stats.files_done == 1
    rows = {
        Path(row["file_path"]).name: (row["status"], row["status_reason"])
        for row in store.conn.execute("SELECT * FROM files")
    }
    assert rows["locked.docx"] == ("unscannable", "encrypted")


def test_server_failure_requeues_files(store: Store, client: ServerClient, config: Config, corpus: Path, monkeypatch):
    from detect_core.contracts import ScanPayload

    payload = ScanPayload(scan_id="local-fail", roots=[str(corpus)], force=False)
    store.create_scan(payload, None)
    run_crawl(store, "local-fail")

    monkeypatch.setenv("MOCK_FAIL_RATE", "1")             # every /detect returns 503
    stats = Processor(store, client, config.device_id).process_once("local-fail")
    assert stats is not None and stats.files_requeued == 3
    assert store.scan_counts("local-fail")["files_done"] == 0

    monkeypatch.setenv("MOCK_FAIL_RATE", "0")
    stats = Processor(store, client, config.device_id).run_until_idle("local-fail")
    assert stats.files_done == 3                          # retried successfully


def test_ocr_failure_marks_the_image_unscannable(store: Store, client: ServerClient, config: Config, tmp_path: Path, monkeypatch):
    from detect_core.contracts import ScanPayload
    from tests.conftest import make_png

    root = tmp_path / "images"
    root.mkdir()
    make_png(root / "kyc_scan.png")

    payload = ScanPayload(scan_id="local-ocr", roots=[str(root)], force=False)
    store.create_scan(payload, None)
    run_crawl(store, "local-ocr")

    monkeypatch.setenv("MOCK_OCR_FAIL", "1")
    stats = Processor(store, client, config.device_id).run_until_idle("local-ocr")
    assert stats.files_unscannable == 1
    row = store.conn.execute("SELECT * FROM files").fetchone()
    assert (row["status"], row["status_reason"]) == ("unscannable", "ocr_failed")


# --------------------------------------------------------------------------- #
# reporter
# --------------------------------------------------------------------------- #

def test_reporter_sends_progress_then_complete(store: Store, client: ServerClient, config: Config, corpus: Path):
    poller = Poller(store, client, config, threading.Event())
    poller.poll_once()
    scan_id = create_server_scan(client, [str(corpus)])
    poller.poll_once()

    reporter = Reporter(store, client, config, threading.Event())
    run_crawl(store, scan_id)
    reporter.tick(final=True)                              # progress while files remain
    view = client._client.get(f"/admin/scans/{scan_id}").json()
    assert view["status"] == "running" and view["files_discovered"] == 3

    Processor(store, client, config.device_id).run_until_idle(scan_id)
    reporter.tick(final=True)                              # now it completes
    view = client._client.get(f"/admin/scans/{scan_id}").json()
    assert view["status"] == "completed"
    assert view["files_done"] == 3 and view["findings"] == 3
    assert view["findings_by_tier"] == {"confidential": 3}
    assert store.get_scan(scan_id)["status"] == "completed"


def test_reporter_never_reports_local_scans(store: Store, client: ServerClient, config: Config, corpus: Path):
    from detect_core.contracts import ScanPayload

    payload = ScanPayload(scan_id="local-quiet", roots=[str(corpus)], force=False)
    store.create_scan(payload, None)
    run_crawl(store, "local-quiet")
    Processor(store, client, config.device_id).run_until_idle("local-quiet")

    Reporter(store, client, config, threading.Event()).tick(final=True)
    assert client._client.get("/admin/scans").json() == []   # nothing was reported
    assert store.get_scan("local-quiet")["status"] == "completed"


def test_heartbeat_reports_the_lifetime_file_count(store: Store, client: ServerClient, config: Config, corpus: Path):
    from detect_core.contracts import ScanPayload

    payload = ScanPayload(scan_id="local-hb", roots=[str(corpus)], force=False)
    store.create_scan(payload, None)
    run_crawl(store, "local-hb")
    Processor(store, client, config.device_id).run_until_idle("local-hb")
    Reporter(store, client, config, threading.Event()).tick(final=True)

    devices = client._client.get("/admin/devices").json()
    device = next(d for d in devices if d["device_id"] == config.device_id)
    assert device["files_scanned"] >= 3
    assert device["agent_version"] == config.agent_version
