# CLAUDE.md — kavach-agent

The laptop agent. It **discovers files, parses them and extracts text**, sends that text to
the server for detection, and stores the returned findings as metadata. Nothing else.

Contract: `../api.md` (v1A.1, frozen 13:50 IST 26 Sep 2026) is the source of truth for the
HTTP behaviour; `detect_core.contracts` is the source of truth for the data models.

## Non-negotiable rules (copied from the workspace root)

0. **Phase 1 (1A/1B): no ML and no OCR on the laptop.** The agent only discovers, parses and
   extracts text. Scanned PDF pages and images go to the server's `POST /ocr`. No Ollama,
   Tesseract, torch or `detect-core[llm]` in this repo until Phase 2.
1. **Never persist or log a raw identifier value**: not in SQLite, traces, logs, exceptions
   or eval output. Store only `masked_value` (last 4 chars visible) and
   `value_hash = HMAC-SHA256(ORG_SALT, normalised_value)`. Turn exceptions into strings with
   `agent.util.safe_error` — it is the only approved path, and it redacts digit runs.
2. **Never log or persist API keys.**
3. **Deterministic maths stays in code** (server side in 1A).
4. **The LLM never sees candidate digits** (server side).
5. **Final tier is computed by code** on the server; the agent only stores what it is given.
6. **Contracts are frozen after T0**: `contracts.py`, `server/schema.sql`, `api.md`. An
   unavoidable change goes under "Contract changes" in `BUILD_PLAN.md` and into both repos
   in the same step.
7. **No hardcoded outputs.** Never special-case corpus file names or values.
8. Config via env vars only (`.env.example`). Never commit `.env`.
9. Commit after each task: `T<id>: <what>`. Run `pytest -q` first.

## This repo

| Module | Role |
|---|---|
| `agent/config.py` | env-only configuration |
| `agent/util.py` | time, hashing, logging, `safe_error` |
| `agent/store.py` | SQLite (WAL, one connection per thread), atomic claim, recovery |
| `agent/client.py` | httpx wrapper, one method per endpoint, retry/backoff/timeouts |
| `agent/crawler.py` | walk, filter, hash, `folder_class`, unchanged-skip |
| `agent/extractors.py` | text/docx/pdf/sheet/image chunking exactly as in api.md §7.3 |
| `agent/processor.py` | claim → extract → `/ocr` → `/detect` → store findings |
| `agent/main.py` | `run` (Poller, Crawler, Processor ×2, Reporter) and `scan <roots>` |
| `tools/mock_server.py` | contract-faithful mock server; **not** part of the agent |

Not built yet: `agent/actions.py` and `agent/ui.py` (Phase 1B), `agent/local/` (Phase 2).

## Where the text lives

Extracted text exists only in memory, inside `Processor._send`, for the duration of one
`POST /detect`. It is never written to SQLite, never logged, and dropped as soon as the
response is parsed. `agent/schema.sql` deliberately has no column that could hold it.

## Conventions

- Type hints everywhere; pydantic models for all I/O; no regex-based detection in this repo.
- Every new behaviour needs a test. `tests/test_privacy.py` must stay green.
- Keep `detect_core` imports to `detect_core.contracts` — the agent must work without the
  `[llm]` extra installed.
