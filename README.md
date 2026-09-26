# kavach-agent — Phase 1A

The laptop agent: it finds files, parses them, extracts text, and sends that text to the
Kavach server, which does all the detection. The agent stores only finding **metadata**
(masked value + hash) in a local SQLite database.

**No detection, no ML and no OCR run on the laptop.** Images and text-less PDF pages are
rendered and posted to the server's `/ocr`.

## Install

```bash
python -m venv .venv && . .venv/Scripts/activate      # Windows; use bin/activate elsewhere
pip install -r requirements.txt                        # includes -e ../kavach-server/detect_core
cp .env.example .env                                   # then edit SERVER_URL and DEVICE_ID
```

If the server repo is not checked out next to this one:

```bash
pip install "detect-core @ git+<server repo url>#subdirectory=detect_core"
```

## Run

```bash
# one-shot scan (the CP1 command)
python -m agent.main scan ~/demo_folder [--force]

# the daemon: polls for SCAN commands, crawls, extracts, reports
python -m agent.main run

# helpers
python -m agent.main health      # is the server reachable?
python -m agent.main status      # what the local DB knows
```

`scan` prints files scanned, findings by tier, pending and seconds taken.

## Develop against the mock server

`tools/mock_server.py` implements the api.md contract with in-memory state and synthetic
findings, so the agent can be built and tested before the real server exists. It rejects any
request that does not match `detect_core.contracts` with a 422, so contract drift shows up
immediately.

```bash
PYTHONPATH=tools python -m uvicorn mock_server:app --host 0.0.0.0 --port 8000

# in another shell: the agent must poll once before a scan can be queued for it
SERVER_URL=http://localhost:8000 POLL_INTERVAL_SEC=2 python -m agent.main run

curl -X POST localhost:8000/admin/scans -H 'Content-Type: application/json' \
  -d '{"device_id":"LAPTOP-01","roots":["/abs/path/to/demo_folder"],"force":true}'
curl localhost:8000/admin/scans
```

Mock knobs (env): `MOCK_FINDINGS_PER_FILE`, `MOCK_OCR_FAIL=1`, `MOCK_FAIL_RATE=0.5`,
`MOCK_FAIL_ONCE_ON=detect,ocr`, `COMMAND_REDELIVERY_SEC`.

## How it works

```
Poller     GET /devices/{id}/commands/next → dedupe by command_id → ack → queue scan
Crawler    oldest queued scan: os.walk, filter, sha256, folder_class, unchanged-skip
Processor  claims ≤20 files → extract → /ocr for images & scanned pages → POST /detect
  ×2       → store findings (metadata only) → mark files done/unscannable
Reporter   POST /scans/{id}/progress every 5 s · /heartbeat every 30 s · /complete once
```

One process, one SQLite database in WAL mode, one connection per thread. Claiming files is
atomic, so the two processors never collide. On startup, `processing` rows are returned to
`discovered` and a `running` scan resumes.

### Chunking (api.md §7.3)

| Source | Chunks |
|---|---|
| txt/md/log/json/env/ini/yaml/yml/xml/eml | whole file, ≤ 4,000 chars, 150-char overlap |
| docx | paragraphs, then table rows (cells joined with ` \| `) |
| pdf page with ≥ 20 chars | per page, chunked like text, `page` set |
| pdf page with < 20 chars | rendered at 150 dpi → `/ocr` (max 10 pages, then `ocr_page_cap`) |
| png/jpg/jpeg/tiff | `/ocr`, `page=1`, `ocr_confidence` set |
| csv/tsv/xlsx | **column chunks**: per 50-row block × column, one cell value per line, with `row`, `column`, `column_header`; `page` = sheet index |

Caps: 1,000 chunks per file and 5,000 data rows per sheet → `status_reason="truncated"`.
Batches: ≤ 20 files, ≤ 2,000 chunks, ≤ 8 MB, and a file is never split across requests.

### Unscannable files

`encrypted`, `corrupt`, `permission_denied`, `oversize`, `ocr_failed` — the file still goes
to `/detect` as a `FileMeta` with no chunks, so the server records that it exists.

## Privacy

- The agent database has **no column that can hold text** (`agent/schema.sql`).
- Extracted text exists only in memory during one `/detect` call.
- Exceptions are stored through `safe_error()`, which truncates to 200 characters and
  replaces every run of 4+ digits.
- `tests/test_privacy.py` scans the database bytes, every text column and the log stream for
  the corpus values in every spacing variant. Point it at a real corpus with
  `CORPUS_LABELS=/path/to/labels.jsonl`.

## Building a standalone executable

The agent normally ships as a Python install (above).  For a laptop without
Python, `kavach-agent.spec` builds a self-contained executable:

```bash
pip install pyinstaller
pyinstaller kavach-agent.spec --noconfirm                 # one file  -> dist/kavach-agent.exe
KAVACH_ONEDIR=1 pyinstaller kavach-agent.spec --noconfirm # one folder -> dist/kavach-agent/
```

| | size | cold start | `scan` of the 8-file demo folder |
|---|---|---|---|
| one-file | 45 MB | ~5 s | ~6 s |
| one-folder | 83 MB | ~2.7 s | ~2.2 s |

A one-file exe unpacks itself to a temp directory on every launch, which is
where the extra seconds go.  **Prefer one-folder for the demo.**

Ship the executable (or the folder) with a `.env` beside it:

```
kavach-agent.exe
.env                 SERVER_URL=http://<server-ip>:8000, DEVICE_ID=LAPTOP-01
agent.db             created on first run, next to the executable
```

`agent/config.py::_base_dir()` anchors `.env` and a relative `AGENT_DB` to the
folder holding the executable when frozen, and to the repo root otherwise. Without
that, a one-file build would read a `.env` that never exists and write its database
into the temp directory, losing unchanged-skip and crash recovery on every launch.

Three things the spec has to handle, all verified by building and running it:

- `agent/schema.sql` is a data file (`--add-data`), not a module.
- pdfplumber needs the `pypdfium2` native renderer and pdfminer's cmap tables;
  the spec collects both. Without them, PDF pages fail at runtime, not at build.
- **detect-core is installed editable, so PyInstaller cannot see it.** The spec
  puts `../kavach-server/detect_core` on `pathex` and names `detect_core` as a
  hidden import. Override the location with `DETECT_CORE_SRC=/path/to/detect_core`.

Streamlit, pandas, FastAPI and the test tooling are excluded on purpose: the 1A
agent does not import them, and they roughly triple the size. Rule 0 still holds -
no OCR or ML libraries are bundled.

The executable is unsigned, so SmartScreen will warn on a machine that has never
run it, and the first launch is slower while Defender scans it.

## Tests

```bash
pytest -q          # 88 passing, 1 skipped (symlink test needs privileges on Windows)
```

## Status

Phase 1A is complete: poll, ack, crawl, extract, `/ocr`, `/detect`, progress, complete,
heartbeat, CLI scan, retries and crash recovery.

Not built: `agent/actions.py` and `agent/ui.py` (Phase 1B), `agent/local/` (Phase 2).

### Integration notes for the server (Person B)

- `detect_core/detect_core/contracts.py` in this workspace was written from `api.md` + the
  T0 spec so the agent could be built. **Diff it against your copy before CP-B** — the two
  must be byte-identical.
- CLI scans send `scan_id="local-<uuid4>"` on `/detect` and never call `/progress` or
  `/complete`. If the server rejects unknown scan ids, set `SEND_LOCAL_SCAN_ID=0`.
- `status_reason` is a free-form string on the wire (a new reason on one side must not 422
  the other). The agent only emits: `encrypted`, `corrupt`, `permission_denied`, `oversize`,
  `unsupported`, `extract_failed`, `ocr_failed`, `ocr_page_cap`, `truncated`, `unchanged`.
