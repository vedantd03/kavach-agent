"""Resource benchmarks: what the agent costs on someone's laptop.

Excluded from the default run (they write ~100 MB of scratch files and take
~20 s).  Run them explicitly:

    pytest -m benchmark -s              # -s to see the table

Each one asserts a ceiling as well as printing a number, so a regression in
parsing or batching fails instead of being noticed six months later.  The
ceilings are deliberately loose - they catch "we now hold ten times as much",
not a 5% drift on a busier machine.  Override any of them with the env vars
named in ``LIMITS`` if your hardware disagrees.
"""

from __future__ import annotations

import gc
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from detect_core.contracts import MAX_FILES_PER_BATCH, ScanPayload

from agent.crawler import run_crawl
from agent.extractors import FileFacts, extract_file
from agent.processor import Processor
from agent.store import Store
from detect_core.contracts import classify_folder, file_type_for

pytestmark = pytest.mark.benchmark

psutil = pytest.importorskip("psutil", reason="pip install -r requirements-dev.txt")

MB = 1024 * 1024

#: env var -> default.  All sizes in MB, throughput in MB/s.
LIMITS = {
    "BENCH_BIG_FILE_MB": 32,           # single-file parse test size
    "BENCH_BATCH_FILE_MB": 4,          # per-file size in the 20-file batch test
    "BENCH_MAX_SINGLE_MEM_MB": 160,    # peak delta parsing one big file
    "BENCH_MAX_BATCH_MEM_MB": 220,     # peak delta extracting a full 20-file batch
    "BENCH_MAX_SHEET_MEM_MB": 40,      # peak delta parsing a 20k-row csv
    "BENCH_MIN_PARSE_MBPS": 5,         # text parse throughput floor
    "BENCH_MAX_CPU_MS_PER_FILE": 250,  # agent-side CPU per file, end to end
}


def limit(name: str) -> float:
    return float(os.environ.get(name, LIMITS[name]))


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #

@dataclass
class Measurement:
    label: str
    wall_s: float
    cpu_s: float
    peak_delta_mb: float
    retained_mb: float
    detail: str = ""

    def report(self) -> str:
        return (
            f"  {self.label:<42} wall={self.wall_s:6.2f}s  cpu={self.cpu_s:6.2f}s  "
            f"peak=+{self.peak_delta_mb:6.1f} MB  retained={self.retained_mb:6.1f} MB  {self.detail}"
        )


class _PeakSampler(threading.Thread):
    """Poll RSS on a background thread; GC-independent, unlike tracemalloc."""

    def __init__(self, process: "psutil.Process", interval: float = 0.02) -> None:
        super().__init__(daemon=True)
        self.process = process
        self.interval = interval
        self.peak = 0
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.peak = max(self.peak, self.process.memory_info().rss)
            except Exception:                    # noqa: BLE001 - process info can blip
                pass
            time.sleep(self.interval)

    def stop(self) -> int:
        self._stop.set()
        self.join(timeout=2)
        return self.peak


def measure(label: str, fn: Callable[[], str]) -> Measurement:
    process = psutil.Process()
    gc.collect()
    base_rss = process.memory_info().rss
    sampler = _PeakSampler(process)
    sampler.start()

    cpu0, wall0 = time.process_time(), time.monotonic()
    detail = fn() or ""
    cpu_s, wall_s = time.process_time() - cpu0, time.monotonic() - wall0

    peak = max(sampler.stop(), base_rss)
    gc.collect()
    retained = process.memory_info().rss - base_rss

    result = Measurement(label, wall_s, cpu_s, (peak - base_rss) / MB, retained / MB, detail)
    print("\n" + result.report())
    return result


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

LINE = ("some ordinary document text with an id 12345 and a name in it " * 4) + "\n"


def write_text_file(path: Path, size_mb: float) -> Path:
    target = int(size_mb * MB)
    with open(path, "w", encoding="utf-8") as handle:
        written = 0
        while written < target:
            handle.write(LINE)
            written += len(LINE)
    return path


def facts_for(path: Path) -> FileFacts:
    return FileFacts(str(path), "h" * 64, file_type_for(str(path)), classify_folder(str(path)))


def parse(path: Path) -> str:
    result = extract_file(str(path), facts_for(path), ocr=None)
    return f"{len(result.chunks)} chunks, reason={result.status_reason}"


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def test_text_parse_throughput(tmp_path: Path):
    """Parsing must stay I/O-bound, not turn quadratic in file size."""
    size_mb = limit("BENCH_BIG_FILE_MB")
    path = write_text_file(tmp_path / "big.txt", size_mb)

    result = measure(f"parse {size_mb:.0f} MB txt", lambda: parse(path))

    throughput = size_mb / max(result.wall_s, 1e-6)
    print(f"  {'':<42} throughput={throughput:.1f} MB/s")
    assert throughput >= limit("BENCH_MIN_PARSE_MBPS"), (
        f"text parsing dropped to {throughput:.1f} MB/s"
    )


def test_single_large_file_memory_is_bounded(tmp_path: Path):
    """A big file is read whole, so the spike scales with its size - but only once.

    ``max_file_mb`` (default 50) is what ultimately caps this on a laptop.
    """
    size_mb = limit("BENCH_BIG_FILE_MB")
    path = write_text_file(tmp_path / "big.txt", size_mb)

    result = measure(f"memory: one {size_mb:.0f} MB file", lambda: parse(path))

    assert result.peak_delta_mb <= limit("BENCH_MAX_SINGLE_MEM_MB")
    assert result.retained_mb <= 20, "parsing one file should not retain memory"


def test_full_batch_extraction_memory(tmp_path: Path):
    """The ceiling that matters: Processor extracts 20 files before sending any.

    Every chunk of all 20 is held at once, so peak memory tracks the batch's
    total text, not the 8 MB request cap.  If this number ever drops sharply,
    extraction went lazy - update the ceiling to lock the win in.
    """
    per_file_mb = limit("BENCH_BATCH_FILE_MB")
    paths = [
        write_text_file(tmp_path / f"f{i}.txt", per_file_mb)
        for i in range(MAX_FILES_PER_BATCH)
    ]

    def extract_batch() -> str:
        held = [extract_file(str(p), facts_for(p), ocr=None) for p in paths]
        chunks = sum(len(r.chunks) for r in held)
        chars = sum(len(c.text) for r in held for c in r.chunks)
        return f"{len(held)} files, {chunks} chunks, {chars / 1e6:.1f}M chars held"

    result = measure(
        f"memory: {MAX_FILES_PER_BATCH}-file batch ({per_file_mb:.0f} MB each)", extract_batch
    )

    assert result.peak_delta_mb <= limit("BENCH_MAX_BATCH_MEM_MB"), (
        "batch extraction is holding more than expected; see Processor.process_once"
    )


def test_sheet_row_cap_keeps_memory_flat(tmp_path: Path):
    """The 5,000-row cap must bound memory however long the sheet is."""
    path = tmp_path / "wide.csv"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("name,email,mobile,aadhaar,address\n")
        for i in range(20_000):
            handle.write(f"Person {i},p{i}@example.in,98765{i:05d},2345678{i:05d},Street {i}\n")

    result = measure("memory: 20k-row csv (5k cap)", lambda: parse(path))

    assert result.peak_delta_mb <= limit("BENCH_MAX_SHEET_MEM_MB")


# --------------------------------------------------------------------------- #
# crawling
# --------------------------------------------------------------------------- #

def test_crawl_throughput(tmp_path: Path, store: Store):
    """Discovery hashes every file, so it is bound by file opens, not by us.

    Profiling shows ~93% of this is ``_io.open`` - on Windows that is the
    real-time antivirus scanning each file as we read it, which is why the rate
    swings between roughly 75 and 300 files/s depending on cache warmth.  The
    floor below is a "something is badly wrong" guard, not a performance target.

    Consequence worth remembering: a laptop with 50,000 matching files spends
    several minutes in the crawl alone.  A (size, mtime) pre-check before
    hashing would make *repeat* scans nearly free; first scans are stuck with
    reading every byte.
    """
    root = tmp_path / "many"
    root.mkdir()
    count = 300
    for index in range(count):
        (root / f"f{index}.txt").write_text(f"file {index}\n" * 20, encoding="utf-8")

    payload = ScanPayload(scan_id="bench-crawl", roots=[str(root)], force=True)
    store.create_scan(payload, None)

    result = measure(
        f"crawl {count} small files",
        lambda: f"{run_crawl(store, 'bench-crawl').discovered} discovered",
    )

    rate = count / max(result.wall_s, 1e-6)
    print(f"  {'':<42} {rate:.0f} files/s")
    assert rate >= 50, f"crawl slowed to {rate:.0f} files/s"


# --------------------------------------------------------------------------- #
# end to end (mock server, so this is agent-side cost only)
# --------------------------------------------------------------------------- #

def test_end_to_end_scan_cost(tmp_path: Path, store: Store, client, config):
    """CPU and memory for a realistic scan, with the server cost factored out.

    The mock answers instantly, so what is left is what the agent itself burns:
    walk, hash, parse, chunk, serialise, store.
    """
    root = tmp_path / "corpus"
    root.mkdir()
    files = 40
    for index in range(files):
        write_text_file(root / f"doc{index}.txt", 0.25)

    payload = ScanPayload(scan_id="bench-e2e", roots=[str(root)], force=True)
    store.create_scan(payload, None)
    run_crawl(store, "bench-e2e")

    processor = Processor(store, client, config.device_id)

    def run() -> str:
        stats = processor.run_until_idle("bench-e2e")
        return f"{stats.files_done} files, {stats.chunks_sent} chunks, {stats.batches} batches"

    result = measure(f"end-to-end scan of {files} files", run)

    assert store.scan_counts("bench-e2e")["files_done"] == files
    cpu_ms_per_file = (result.cpu_s * 1000) / files
    print(f"  {'':<42} {cpu_ms_per_file:.0f} ms CPU per file")
    assert cpu_ms_per_file <= limit("BENCH_MAX_CPU_MS_PER_FILE"), (
        f"agent burns {cpu_ms_per_file:.0f} ms CPU per file"
    )


def test_idle_daemon_is_cheap(store: Store, client, config):
    """An idle Processor must not spin: no work claimed, no CPU burned."""
    processor = Processor(store, client, config.device_id)

    def idle() -> str:
        polls = 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            assert processor.process_once() is None       # nothing to claim
            polls += 1
            time.sleep(0.05)
        return f"{polls} empty claim cycles"

    result = measure("idle processor (2 s)", idle)

    assert result.cpu_s <= 0.5, "an idle processor should be almost free"
