"""Agent entry points (api.md 8).

    python -m agent.main run                 daemon: Poller, Crawler, Processor x2, Reporter
    python -m agent.main scan <roots...>     one-shot local scan (the CP1 command)
    python -m agent.main status              what the local database knows
    python -m agent.main health              is the server reachable?

The daemon is four kinds of thread over one SQLite database:

  Poller     asks for commands, acks them, queues scans
  Crawler    turns the oldest queued scan into ``files`` rows
  Processor  claims files, extracts text, calls /detect, stores findings
  Reporter   sends progress every 5 s, heartbeat every 30 s, complete once
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from typing import Optional

from detect_core.contracts import ScanComplete, ScanPayload, ScanProgress

from agent.client import NotFound, ScanClosed, ServerClient, ServerError
from agent.config import Config, load_config
from agent.crawler import existing_roots, run_crawl
from agent.processor import Processor, ProcessStats
from agent.store import (
    SCAN_STATUS_COMPLETED,
    SCAN_STATUS_FAILED,
    Store,
)
from agent.util import get_logger, is_local_scan, local_scan_id, safe_error, setup_logging

log = get_logger("main")

IDLE_SLEEP_SEC = 1.0


# --------------------------------------------------------------------------- #
# threads
# --------------------------------------------------------------------------- #

class Poller(threading.Thread):
    """Ask for the next command, dedupe it, ack it, queue the scan."""

    def __init__(self, store: Store, client: ServerClient, config: Config, stop: threading.Event):
        super().__init__(name="poller", daemon=True)
        self.store, self.client, self.config, self.stop = store, client, config, stop

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.poll_once()
            except ServerError as exc:
                log.warning("poll failed: %s", exc)
            except Exception as exc:                             # noqa: BLE001
                log.error("poller error: %s", safe_error(exc))
            self.stop.wait(self.config.poll_interval_sec)

    def poll_once(self) -> Optional[str]:
        """Returns the scan_id queued by this poll, if any."""
        command = self.client.next_command()
        if command is None:
            return None

        known = self.store.get_command(command.command_id)
        if known is not None:
            # Redelivery: replay the stored ack verbatim, do nothing else.
            log.info("command %s already handled, re-acking %s",
                     command.command_id, known["ack_status"])
            self._ack(command.command_id, known["ack_status"], known["ack_reason"])
            return None

        payload_json = command.payload.model_dump(mode="json")

        if command.type != "SCAN":
            self.store.record_command(
                command.command_id, command.type, payload_json, "rejected", "UNSUPPORTED_COMMAND"
            )
            self._ack(command.command_id, "rejected", "UNSUPPORTED_COMMAND")
            return None

        present, missing = existing_roots(command.payload.roots)
        if not present:
            self.store.record_command(
                command.command_id, command.type, payload_json, "rejected", "PATH_NOT_FOUND"
            )
            self._ack(command.command_id, "rejected", "PATH_NOT_FOUND")
            log.warning("rejected %s: no root exists", command.command_id)
            return None

        if missing:
            log.warning("scanning %d of %d roots; missing: %s",
                        len(present), len(command.payload.roots), missing)

        payload = command.payload.model_copy(update={"roots": present})
        self.store.create_scan(payload, command.command_id)
        self.store.record_command(command.command_id, command.type, payload_json, "accepted", None)
        self._ack(command.command_id, "accepted", None)
        log.info("queued scan %s (%d roots)", payload.scan_id, len(present))
        return payload.scan_id

    def _ack(self, command_id: str, status: str, reason: Optional[str]) -> None:
        try:
            self.client.ack_command(command_id, status, reason)
        except NotFound:
            log.warning("ack for unknown command %s", command_id)
        except ServerError as exc:
            # The command stays recorded; redelivery will replay this ack.
            log.warning("ack failed for %s: %s", command_id, exc)


class CrawlerThread(threading.Thread):
    """One scan at a time, FIFO: crawl the oldest queued scan."""

    def __init__(self, store: Store, stop: threading.Event):
        super().__init__(name="crawler", daemon=True)
        self.store, self.stop = store, stop

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                if not self.store.has_active_scan():
                    row = self.store.next_queued_scan()
                    if row is not None:
                        run_crawl(self.store, row["scan_id"], self.stop)
                        continue
            except Exception as exc:                             # noqa: BLE001
                log.error("crawler error: %s", safe_error(exc))
            self.stop.wait(IDLE_SLEEP_SEC)


class ProcessorThread(threading.Thread):
    def __init__(self, processor: Processor, stop: threading.Event, index: int):
        super().__init__(name=f"processor-{index}", daemon=True)
        self.processor, self.stop = processor, stop

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                stats = self.processor.process_once()
            except Exception as exc:                             # noqa: BLE001
                log.error("processor error: %s", safe_error(exc))
                stats = None
            if stats is None:
                self.stop.wait(IDLE_SLEEP_SEC)


class Reporter(threading.Thread):
    """Progress every 5 s, heartbeat every 30 s, complete once per scan."""

    def __init__(self, store: Store, client: ServerClient, config: Config, stop: threading.Event):
        super().__init__(name="reporter", daemon=True)
        self.store, self.client, self.config, self.stop = store, client, config, stop
        self._last_progress = 0.0
        self._last_heartbeat = 0.0
        self._closed: set[str] = set()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:                             # noqa: BLE001
                log.error("reporter error: %s", safe_error(exc))
            self.stop.wait(IDLE_SLEEP_SEC)
        self.tick(final=True)

    def tick(self, final: bool = False) -> None:
        now = time.monotonic()
        due = final or (now - self._last_progress >= self.config.progress_interval_sec)

        for row in self.store.running_scans():
            scan_id = row["scan_id"]
            finished = self.store.scan_is_finished(scan_id)
            if finished:
                self._complete(scan_id, row)
            elif due:
                self._progress(scan_id)
        if due:
            self._last_progress = now

        if final or now - self._last_heartbeat >= self.config.heartbeat_interval_sec:
            self._heartbeat()
            self._last_heartbeat = now

    def _progress(self, scan_id: str) -> None:
        if is_local_scan(scan_id) or scan_id in self._closed:
            return
        counts = self.store.scan_counts(scan_id)
        row = self.store.get_scan(scan_id)
        progress = ScanProgress(**counts, crawl_complete=bool(row["crawl_complete"]) if row else False)
        try:
            self.client.progress(scan_id, progress)
        except ScanClosed:
            log.warning("server closed scan %s; no more progress", scan_id)
            self._closed.add(scan_id)
        except NotFound:
            log.warning("server does not know scan %s", scan_id)
            self._closed.add(scan_id)
        except ServerError as exc:
            log.warning("progress failed: %s", exc)

    def _complete(self, scan_id: str, row) -> None:                          # type: ignore[no-untyped-def]
        counts = self.store.scan_counts(scan_id)
        duration = _duration_ms(row["started_at"])
        if not is_local_scan(scan_id) and scan_id not in self._closed:
            payload = ScanComplete(**counts, status="completed", error=None, duration_ms=duration)
            try:
                self.client.complete(scan_id, payload)
            except ServerError as exc:
                log.warning("complete failed for %s: %s", scan_id, exc)
                return
        self.store.finish_scan(scan_id, SCAN_STATUS_COMPLETED)
        log.info("scan %s completed: %s", scan_id, counts)
        self._heartbeat()

    def _heartbeat(self) -> None:
        try:
            self.client.heartbeat(self.store.files_scanned_total())
        except ServerError as exc:
            log.debug("heartbeat failed: %s", exc)


def _duration_ms(started_at: Optional[str]) -> Optional[int]:
    if not started_at:
        return None
    from datetime import datetime, timezone
    try:
        start = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int((datetime.now(timezone.utc) - start).total_seconds() * 1000)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_run(config: Config) -> int:
    store = Store(config.db_file)
    store.recover_processing()
    stop = threading.Event()

    with ServerClient(config) as client:
        threads = [
            Poller(store, client, config, stop),
            CrawlerThread(store, stop),
            Reporter(store, client, config, stop),
        ]
        for index in range(max(1, config.processor_threads)):
            processor = Processor(
                store, client, config.device_id,
                send_local_scan_id=config.send_local_scan_id, stop=stop,
            )
            threads.append(ProcessorThread(processor, stop, index + 1))

        def _shutdown(*_args: object) -> None:
            log.info("shutting down")
            stop.set()

        signal.signal(signal.SIGINT, _shutdown)
        try:
            signal.signal(signal.SIGTERM, _shutdown)
        except (AttributeError, ValueError):                     # not available everywhere
            pass

        log.info("agent %s started, device=%s server=%s",
                 config.agent_version, config.device_id, config.server_url)
        for thread in threads:
            thread.start()
        try:
            while not stop.is_set():
                stop.wait(0.5)
        except KeyboardInterrupt:
            stop.set()
        for thread in threads:
            thread.join(timeout=10)
    store.close()
    return 0


def cmd_scan(config: Config, roots: list[str], force: bool) -> int:
    """Crawl and process inline until the scan is done (the CP1 command)."""
    present, missing = existing_roots(roots)
    for root in missing:
        print(f"warning: root does not exist, skipping: {root}", file=sys.stderr)
    if not present:
        print("error: no root path exists", file=sys.stderr)
        return 2

    store = Store(config.db_file)
    store.recover_processing()
    stop = threading.Event()
    scan_id = local_scan_id()
    payload = ScanPayload(
        scan_id=scan_id,
        roots=present,
        force=force,
        include_types=config.include_types,
        exclude_dirs=config.exclude_dirs,
        max_file_mb=config.max_file_mb,
    )
    store.create_scan(payload, None)

    started = time.monotonic()
    with ServerClient(config) as client:
        try:
            crawl = run_crawl(store, scan_id, stop)
        except Exception as exc:                                 # noqa: BLE001
            print(f"crawl failed: {safe_error(exc)}", file=sys.stderr)
            return 1

        processor = Processor(
            store, client, config.device_id,
            send_local_scan_id=config.send_local_scan_id, stop=stop,
        )
        stats = processor.run_until_idle(scan_id)
        store.finish_scan(scan_id, SCAN_STATUS_COMPLETED)
        try:
            client.heartbeat(store.files_scanned_total())
        except ServerError as exc:
            print(f"warning: heartbeat failed: {exc}", file=sys.stderr)

    elapsed = time.monotonic() - started
    _print_scan_summary(store, scan_id, crawl, stats, elapsed)
    store.close()
    return 0


def _print_scan_summary(store: Store, scan_id: str, crawl, stats: ProcessStats, elapsed: float) -> None:  # type: ignore[no-untyped-def]
    counts = store.scan_counts(scan_id)
    tiers = store.findings_by_tier(scan_id)
    print()
    print(f"scan {scan_id}")
    print(f"  files discovered : {counts['files_discovered']}")
    print(f"  files scanned    : {counts['files_done']}")
    print(f"  unscannable      : {counts['files_unscannable']}")
    print(f"  skipped          : {counts['files_skipped']} (unchanged)")
    print(f"  failed           : {counts['files_failed']}")
    print(f"  chunks sent      : {stats.chunks_sent} in {stats.batches} request(s)")
    if stats.ocr_pages:
        print(f"  ocr pages        : {stats.ocr_pages}")
    print(f"  findings         : {counts['findings']}")
    for tier in ("restricted", "confidential", "internal", "public"):
        if tiers.get(tier):
            print(f"      {tier:<13}: {tiers[tier]}")
    print(f"  pending          : {stats.pending}")
    print(f"  seconds          : {elapsed:.1f}")


def cmd_status(config: Config) -> int:
    store = Store(config.db_file)
    scans = store.conn.execute(
        "SELECT scan_id, status, crawl_complete, started_at, finished_at FROM scans "
        "ORDER BY rowid DESC LIMIT 10"
    ).fetchall()
    print(json.dumps({
        "device_id": config.device_id,
        "server_url": config.server_url,
        "db": str(config.db_file),
        "files_scanned_total": store.files_scanned_total(),
        "findings_total": store.findings_count(),
        "findings_by_tier": store.findings_by_tier(),
        "recent_scans": [dict(row) for row in scans],
    }, indent=2))
    store.close()
    return 0


def cmd_health(config: Config) -> int:
    with ServerClient(config) as client:
        try:
            print(json.dumps(client.health(), indent=2))
        except ServerError as exc:
            print(f"server unreachable: {exc}", file=sys.stderr)
            return 1
    return 0


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent.main", description="Kavach laptop agent (Phase 1A)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the polling daemon")

    scan = sub.add_parser("scan", help="scan paths now, without the server queue")
    scan.add_argument("roots", nargs="+", help="directories or files to scan")
    scan.add_argument("--force", action="store_true", help="re-scan unchanged files")

    sub.add_parser("status", help="print local agent state")
    sub.add_parser("health", help="check the server")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    setup_logging(config.log_level)

    if args.command == "run":
        return cmd_run(config)
    if args.command == "scan":
        return cmd_scan(config, args.roots, args.force)
    if args.command == "status":
        return cmd_status(config)
    if args.command == "health":
        return cmd_health(config)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
