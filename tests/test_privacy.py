"""Rule 1: no raw identifier value may ever be persisted or logged by the agent.

After a full scan we search the agent database - bytes and every text column -
for the values planted in the corpus, in every spacing and format they appear
in, and we check the log stream too.  The source files themselves are of course
allowed to contain them; nothing the agent writes may.

``CORPUS_LABELS`` (a labels.jsonl from the server repo) is honoured when set, so
this test can be pointed at the real demo corpus.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest

from detect_core.contracts import ScanPayload

from agent.client import ServerClient
from agent.config import Config
from agent.crawler import run_crawl
from agent.processor import Processor
from agent.store import Store
from agent.util import safe_error
from tests.conftest import AADHAAR_PLAIN, AADHAAR_SPACED, EMAIL, MOBILE, PAN


def variants(value: str) -> set[str]:
    """The same identifier written the ways a file might contain it."""
    bare = value.replace(" ", "").replace("-", "")
    out = {value, bare, bare.lower(), bare.upper()}
    if bare.isdigit() and len(bare) == 12:
        out.add(f"{bare[:4]} {bare[4:8]} {bare[8:]}")
        out.add(f"{bare[:4]}-{bare[4:8]}-{bare[8:]}")
    return {item for item in out if len(item) >= 6}


def corpus_values() -> set[str]:
    values = {AADHAAR_SPACED, AADHAAR_PLAIN, PAN, MOBILE, EMAIL}
    labels = os.environ.get("CORPUS_LABELS")
    if labels and Path(labels).exists():
        for line in Path(labels).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            for key in ("value", "raw", "raw_value"):
                if record.get(key):
                    values.add(str(record[key]))
    return values


def db_text(db_path: Path) -> str:
    """Every text value in every table, concatenated."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    pieces: list[str] = []
    tables = [
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    for table in tables:
        for row in connection.execute(f"SELECT * FROM {table}"):
            pieces.extend(str(value) for value in tuple(row) if value is not None)
    connection.close()
    return "\n".join(pieces)


@pytest.fixture()
def scanned(store: Store, client: ServerClient, config: Config, corpus: Path, caplog):
    """Run a complete local scan of the corpus with logging captured."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    root_logger = logging.getLogger("agent")
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)

    payload = ScanPayload(scan_id="local-privacy", roots=[str(corpus)], force=False)
    store.create_scan(payload, None)
    run_crawl(store, "local-privacy")
    Processor(store, client, config.device_id).run_until_idle("local-privacy")

    root_logger.removeHandler(handler)
    return {"config": config, "store": store, "logs": stream.getvalue()}


def test_scan_actually_ran(scanned):
    store: Store = scanned["store"]
    assert store.scan_counts("local-privacy")["files_done"] == 3
    assert store.findings_count("local-privacy") == 3       # something was stored


def test_no_raw_value_in_the_agent_database(scanned):
    config: Config = scanned["config"]
    raw_bytes = Path(config.db_file).read_bytes()
    text = db_text(Path(config.db_file))

    for value in corpus_values():
        for variant in variants(value):
            assert variant.encode("utf-8") not in raw_bytes, f"{variant!r} found in the DB file"
            assert variant not in text, f"{variant!r} found in a DB column"


def test_no_chunk_text_in_the_agent_database(scanned):
    """A whole sentence from the corpus must not survive anywhere in SQLite."""
    config: Config = scanned["config"]
    text = db_text(Path(config.db_file))
    for phrase in ("bhai mera aadhar", "Cust 0", "cust0@example.in", "nothing here"):
        assert phrase not in text


def test_no_raw_value_in_the_logs(scanned):
    logs: str = scanned["logs"]
    assert logs.strip(), "expected the scan to log something"
    for value in corpus_values():
        for variant in variants(value):
            assert variant not in logs, f"{variant!r} found in the logs"


def test_findings_keep_only_masked_metadata(scanned):
    store: Store = scanned["store"]
    rows = list(store.conn.execute("SELECT * FROM findings"))
    assert rows
    for row in rows:
        assert row["masked_value"] is None or row["masked_value"].count("X") >= 4
        assert row["value_hash"] and len(row["value_hash"]) >= 32


def test_schema_has_no_text_columns():
    """The schema itself must not offer a place to put content."""
    schema = (Path(__file__).resolve().parent.parent / "agent" / "schema.sql").read_text(
        encoding="utf-8"
    )
    lowered = schema.lower()
    for forbidden in (" text_content", " snippet", " chunk_text", " ocr_text", " raw_value"):
        assert forbidden not in lowered


def test_safe_error_redacts_digits_and_truncates():
    """Parser exceptions can quote file content; safe_error is the only way in."""
    exc = ValueError("bad row: aadhaar 2345 6789 4821 in column 3\nnext line")
    message = safe_error(exc)
    assert "2345" not in message and "4821" not in message
    assert "\n" not in message
    assert message.startswith("ValueError: ")

    long_exc = RuntimeError("x" * 500)
    assert len(safe_error(long_exc)) <= 200 + len("RuntimeError: ") + 3
