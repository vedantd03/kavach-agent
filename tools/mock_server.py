"""Mock implementation of the Kavach pii-server HTTP contract (api.md, v1A.1).

This module is a throwaway FastAPI test double for the real `pii-server`,
**not** part of it. It exists only so `kavach-agent` can be built and tested
against something that speaks the exact wire contract defined in
`kavach-server/detect_core/detect_core/contracts.py` before the real server
exists. It lives in `kavach-agent/tools/`, outside the agent package, and the
agent must never import it.

Design rules:
  * Every request/response model is imported from `detect_core.contracts`
    (never redefined here), so a client that drifts from the frozen contract
    gets a loud FastAPI `422` instead of a silent pass.
  * State is in-memory only (plain dicts on a module-level `STATE`
    singleton). No database, no files written to disk.
  * This is a MOCK: `/detect` never inspects chunk text and never runs any
    real detection logic (no regexes, no ML, no LLM calls). `/ocr` never
    looks at pixels. Neither endpoint retains request bytes/text anywhere,
    and request bodies are never logged, matching api.md section 1.

Run directly with `python mock_server.py` (serves on 0.0.0.0:8000), or
`uvicorn mock_server:app --reload` once `detect_core` is importable.

Env knobs:
  AUTH_ENABLED          - accepted, always ignored (1A default: off).
  COMMAND_REDELIVERY_SEC- seconds before an un-acked `delivered` command is
                          redelivered on the next poll. Default 60.
  MOCK_OCR_FAIL         - "1" makes every /ocr call return 503 OCR_UNAVAILABLE.
  MOCK_FINDINGS_PER_FILE- synthetic findings emitted per eligible file in
                          /detect. Default 1, may be 0.
  MOCK_FAIL_RATE        - float in [0, 1]; that fraction of /detect and /ocr
                          calls randomly return 503. Default 0.
  MOCK_FAIL_ONCE_ON     - comma-separated endpoint names ("detect", "ocr")
                          that fail with 503 exactly once, then behave.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import random
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.responses import JSONResponse

# --------------------------------------------------------------------------- #
# Make `detect_core` importable even when the caller hasn't put it on
# PYTHONPATH. Expected layout: BPF/kavach-agent/tools/mock_server.py next to
# a sibling BPF/kavach-server/detect_core/detect_core/contracts.py.
# --------------------------------------------------------------------------- #
if importlib.util.find_spec("detect_core") is None:
    _detect_core_dir = Path(__file__).resolve().parents[2] / "kavach-server" / "detect_core"
    if _detect_core_dir.is_dir():
        sys.path.insert(0, str(_detect_core_dir))

from detect_core.contracts import (  # noqa: E402  (import must follow the sys.path shim)
    CONTRACT_VERSION,
    DEFAULT_EXCLUDE_DIRS,
    DEFAULT_INCLUDE_TYPES,
    DEFAULT_MAX_FILE_MB,
    MAX_CHUNKS_PER_BATCH,
    MAX_FILES_PER_BATCH,
    MAX_OCR_IMAGE_BYTES,
    Command,
    CommandAck,
    CreateScanRequest,
    CreateScanResponse,
    DetectRequest,
    DetectResponse,
    DeviceView,
    Finding,
    HeartbeatRequest,
    OcrResult,
    OkResponse,
    ScanComplete,
    ScanPayload,
    ScanProgress,
    ScanView,
    finding_id_for,
    risk_band_for,
)

POLICY_VERSION = "2026-09-26.1"

# --------------------------------------------------------------------------- #
# In-memory state
# --------------------------------------------------------------------------- #


@dataclass
class _State:
    devices: dict[str, dict] = field(default_factory=dict)
    commands: dict[str, dict] = field(default_factory=dict)
    scans: dict[str, dict] = field(default_factory=dict)
    findings: dict[str, Finding] = field(default_factory=dict)
    traces: list[dict] = field(default_factory=list)
    detect_calls: int = 0
    failed_once: set[str] = field(default_factory=set)

    def reset(self) -> None:
        self.devices.clear()
        self.commands.clear()
        self.scans.clear()
        self.findings.clear()
        self.traces.clear()
        self.detect_calls = 0
        self.failed_once.clear()


STATE = _State()


def _new_device(device_id: str) -> dict:
    return {"device_id": device_id, "last_seen": None, "files_scanned": 0, "agent_version": None}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def api_error(status_code: int, code: str, message: str) -> NoReturn:
    """Raise the `{"detail": {"code", "message"}}` shape from api.md section 1."""
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _command_redelivery_sec() -> float:
    try:
        return float(os.environ.get("COMMAND_REDELIVERY_SEC", "60"))
    except ValueError:
        return 60.0


def _mock_findings_per_file() -> int:
    try:
        return max(0, int(os.environ.get("MOCK_FINDINGS_PER_FILE", "1")))
    except ValueError:
        return 1


def _mock_ocr_always_fails() -> bool:
    return os.environ.get("MOCK_OCR_FAIL", "0").strip().lower() in ("1", "true", "yes")


def _fail_rate() -> float:
    try:
        return float(os.environ.get("MOCK_FAIL_RATE", "0") or 0)
    except ValueError:
        return 0.0


def _fail_once_targets() -> set[str]:
    raw = os.environ.get("MOCK_FAIL_ONCE_ON", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _maybe_chaos(endpoint: str) -> None:
    """Deterministic-ish 503 injection for /detect and /ocr (api.md section 1 retries)."""
    if endpoint in _fail_once_targets() and endpoint not in STATE.failed_once:
        STATE.failed_once.add(endpoint)
        api_error(503, "MOCK_CHAOS_ONCE", f"mock chaos: forced one-time failure on {endpoint}")
    rate = _fail_rate()
    if rate > 0 and random.random() < rate:
        api_error(503, "MOCK_CHAOS_RANDOM", f"mock chaos: random failure on {endpoint}")


def _scan_view(scan: dict) -> ScanView:
    tier_counts: dict[str, int] = {}
    for finding in STATE.findings.values():
        if finding.scan_id == scan["scan_id"]:
            tier_counts[finding.sensitivity_tier] = tier_counts.get(finding.sensitivity_tier, 0) + 1
    return ScanView(
        scan_id=scan["scan_id"],
        command_id=scan.get("command_id"),
        device_id=scan["device_id"],
        roots=list(scan.get("roots", [])),
        force=scan.get("force", False),
        status=scan["status"],
        files_discovered=scan.get("files_discovered", 0),
        files_done=scan.get("files_done", 0),
        files_unscannable=scan.get("files_unscannable", 0),
        files_skipped=scan.get("files_skipped", 0),
        files_failed=scan.get("files_failed", 0),
        findings=scan.get("findings", 0),
        crawl_complete=scan.get("crawl_complete", False),
        findings_by_tier=tier_counts,
        reject_reason=scan.get("reject_reason"),
        error=scan.get("error"),
        duration_ms=scan.get("duration_ms"),
        created_at=iso(scan.get("created_at")),
        started_at=iso(scan.get("started_at")),
        completed_at=iso(scan.get("completed_at")),
    )


# --------------------------------------------------------------------------- #
# Auth (1A: off by default, headers accepted and ignored per api.md section 1)
# --------------------------------------------------------------------------- #


async def device_auth(x_device_token: Optional[str] = Header(default=None)) -> None:
    return None


async def admin_auth(x_admin_token: Optional[str] = Header(default=None)) -> None:
    return None


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = FastAPI(title="Kavach pii-server (MOCK)", version=CONTRACT_VERSION)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "contract_version": CONTRACT_VERSION, "policy_version": POLICY_VERSION}


# --------------------------------------------------------------------------- #
# 3.2-3.3 command polling
# --------------------------------------------------------------------------- #


@app.get(
    "/devices/{device_id}/commands/next",
    responses={204: {"description": "nothing pending"}, 200: {"model": Command}},
)
def commands_next(device_id: str, _auth: None = Depends(device_auth)) -> Response:
    now = utcnow()
    device = STATE.devices.setdefault(device_id, _new_device(device_id))
    device["last_seen"] = now

    redeliver_sec = _command_redelivery_sec()
    candidates = [
        c
        for c in STATE.commands.values()
        if c["device_id"] == device_id
        and (
            c["status"] == "pending"
            or (
                c["status"] == "delivered"
                and c["delivered_at"] is not None
                and (now - c["delivered_at"]).total_seconds() > redeliver_sec
            )
        )
    ]
    if not candidates:
        return Response(status_code=204)

    candidates.sort(key=lambda c: c["created_at"])
    cmd = candidates[0]
    cmd["status"] = "delivered"
    cmd["delivered_at"] = now

    scan = STATE.scans.get(cmd["scan_id"])
    if scan is not None:
        scan["status"] = "delivered"

    model = Command(
        command_id=cmd["command_id"],
        type=cmd["type"],
        created_at=iso(cmd["created_at"]),
        payload=cmd["payload"],
    )
    return JSONResponse(status_code=200, content=model.model_dump(mode="json"))


@app.post("/commands/{command_id}/ack", response_model=OkResponse)
def ack_command(command_id: str, body: CommandAck, _auth: None = Depends(device_auth)) -> OkResponse:
    cmd = STATE.commands.get(command_id)
    if cmd is None:
        api_error(404, "COMMAND_NOT_FOUND", f"no command {command_id}")

    if cmd["status"] in ("accepted", "rejected"):
        return OkResponse(ok=True)  # re-acking is a no-op

    now = utcnow()
    scan = STATE.scans.get(cmd["scan_id"])
    if body.status == "accepted":
        cmd["status"] = "accepted"
        if scan is not None:
            scan["status"] = "running"
            scan["started_at"] = now
    else:
        cmd["status"] = "rejected"
        cmd["reject_reason"] = body.reason
        if scan is not None:
            scan["status"] = "rejected"
            scan["reject_reason"] = body.reason

    return OkResponse(ok=True)


# --------------------------------------------------------------------------- #
# 3.4 OCR
# --------------------------------------------------------------------------- #


@app.post("/ocr", response_model=OcrResult)
async def ocr(
    image: UploadFile = File(...),
    device_id: str = Form(...),
    file_hash: str = Form(...),
    page: Optional[int] = Form(default=None),
    _auth: None = Depends(device_auth),
) -> OcrResult:
    _maybe_chaos("ocr")

    if _mock_ocr_always_fails():
        api_error(503, "OCR_UNAVAILABLE", "mock OCR unavailable (MOCK_OCR_FAIL=1)")

    content_type = (image.content_type or "").lower()
    if content_type not in ("image/png", "image/jpeg"):
        api_error(
            415,
            "UNSUPPORTED_MEDIA",
            f"unsupported content type {content_type!r}; expected image/png or image/jpeg",
        )

    data = await image.read()
    n = len(data)
    del data  # never store the bytes

    if n > MAX_OCR_IMAGE_BYTES:
        api_error(413, "IMAGE_TOO_LARGE", f"image is {n} bytes, exceeds the {MAX_OCR_IMAGE_BYTES}-byte limit")

    STATE.traces.append(
        {"kind": "ocr", "device_id": device_id, "file_hash": file_hash, "page": page, "bytes": n, "at": utcnow()}
    )

    text = f"[mock-ocr device={device_id} page={page} bytes={n}]"
    return OcrResult(text=text, confidence=0.9, pages=1)


# --------------------------------------------------------------------------- #
# 3.5 detect
# --------------------------------------------------------------------------- #


@app.post("/detect", response_model=DetectResponse)
def detect(body: DetectRequest, _auth: None = Depends(device_auth)) -> DetectResponse:
    _maybe_chaos("detect")

    if len(body.files) > MAX_FILES_PER_BATCH or len(body.chunks) > MAX_CHUNKS_PER_BATCH:
        api_error(413, "BATCH_TOO_LARGE", f"batch has {len(body.files)} files / {len(body.chunks)} chunks")

    now = utcnow()
    per_file = _mock_findings_per_file()
    chunked_files = {(c.file_path, c.file_hash) for c in body.chunks}

    findings_out: list[Finding] = []
    ok_files = 0
    for f in body.files:
        if f.status != "ok":
            continue
        ok_files += 1
        if (f.file_path, f.file_hash) not in chunked_files:
            continue
        for i in range(per_file):
            value_hash = hashlib.sha256(f"{f.file_hash}:{i}".encode("utf-8")).hexdigest()
            finding = Finding(
                finding_id=finding_id_for(body.device_id, f.file_hash, "item", "AADHAAR", value_hash),
                device_id=body.device_id,
                scan_id=body.scan_id,
                file_path=f.file_path,
                file_hash=f.file_hash,
                folder_class=f.folder_class,
                file_type=f.file_type,
                location="chunk=0;char=0",
                finding_kind="item",
                category="personal",
                pii_type="AADHAAR",
                doc_type=None,
                masked_value="XXXXXXXX0000",
                value_hash=value_hash,
                holder="individual",
                masked_in_source=False,
                confidence=0.9,
                decided_by="server",
                reason="mock: synthetic finding, no real detection was run",
                llm_suggested_tier="confidential",
                sensitivity_tier="confidential",
                tier_reason="mock",
                policy_version=POLICY_VERSION,
                risk_score=48.0,
                risk_band=risk_band_for(48.0),
                detected_at=iso(now),
            )
            STATE.findings[finding.finding_id] = finding
            findings_out.append(finding)

    device = STATE.devices.setdefault(body.device_id, _new_device(body.device_id))
    device["last_seen"] = now
    device["files_scanned"] += ok_files

    if body.scan_id is not None and body.scan_id in STATE.scans:
        STATE.scans[body.scan_id]["findings"] += len(findings_out)

    STATE.detect_calls += 1

    stats = {
        "files": len(body.files),
        "chunks": len(body.chunks),
        "candidates": len(findings_out),
        "confirmed_by_rules": 0,
        "verified_by_server": len(findings_out),
        "dropped": max(0, len(body.files) - ok_files),
        "pending": 0,
        "llm_calls": 1 if body.chunks else 0,
        "latency_ms": 5,
    }
    return DetectResponse(findings=findings_out, pending=0, stats=stats)


# --------------------------------------------------------------------------- #
# 3.6-3.8 scan progress / complete / heartbeat
# --------------------------------------------------------------------------- #


@app.post("/scans/{scan_id}/progress", response_model=OkResponse)
def scan_progress(scan_id: str, body: ScanProgress, _auth: None = Depends(device_auth)) -> OkResponse:
    scan = STATE.scans.get(scan_id)
    if scan is None:
        api_error(404, "SCAN_NOT_FOUND", f"no scan {scan_id}")
    if scan["status"] in ("completed", "failed", "rejected"):
        api_error(409, "SCAN_CLOSED", f"scan {scan_id} is already {scan['status']}")

    scan["files_discovered"] = body.files_discovered
    scan["files_done"] = body.files_done
    scan["files_unscannable"] = body.files_unscannable
    scan["files_skipped"] = body.files_skipped
    scan["files_failed"] = body.files_failed
    scan["findings"] = body.findings
    scan["crawl_complete"] = body.crawl_complete
    return OkResponse(ok=True)


@app.post("/scans/{scan_id}/complete", response_model=OkResponse)
def scan_complete(scan_id: str, body: ScanComplete, _auth: None = Depends(device_auth)) -> OkResponse:
    scan = STATE.scans.get(scan_id)
    if scan is None:
        api_error(404, "SCAN_NOT_FOUND", f"no scan {scan_id}")

    scan["status"] = body.status
    scan["files_discovered"] = body.files_discovered
    scan["files_done"] = body.files_done
    scan["files_unscannable"] = body.files_unscannable
    scan["files_skipped"] = body.files_skipped
    scan["files_failed"] = body.files_failed
    scan["findings"] = body.findings
    scan["error"] = body.error
    scan["duration_ms"] = body.duration_ms
    scan["completed_at"] = utcnow()
    return OkResponse(ok=True)


@app.post("/heartbeat", response_model=OkResponse)
def heartbeat(body: HeartbeatRequest, _auth: None = Depends(device_auth)) -> OkResponse:
    device = STATE.devices.setdefault(body.device_id, _new_device(body.device_id))
    device["last_seen"] = utcnow()
    if body.agent_version is not None:
        device["agent_version"] = body.agent_version
    device["files_scanned"] = max(device.get("files_scanned", 0), body.files_scanned)
    return OkResponse(ok=True)


# --------------------------------------------------------------------------- #
# 4. Admin endpoints
# --------------------------------------------------------------------------- #


@app.post("/admin/scans", response_model=CreateScanResponse, status_code=201)
def create_scan(body: CreateScanRequest, _auth: None = Depends(admin_auth)) -> CreateScanResponse:
    if body.device_id not in STATE.devices:
        api_error(404, "DEVICE_UNKNOWN", f"device {body.device_id} has never polled")

    now = utcnow()
    scan_id = "scan_" + secrets.token_hex(6)
    command_id = "cmd_" + secrets.token_hex(6)

    payload = ScanPayload(
        scan_id=scan_id,
        roots=body.roots,
        force=body.force,
        include_types=list(body.include_types) if body.include_types is not None else list(DEFAULT_INCLUDE_TYPES),
        exclude_dirs=list(body.exclude_dirs) if body.exclude_dirs is not None else list(DEFAULT_EXCLUDE_DIRS),
        max_file_mb=body.max_file_mb if body.max_file_mb is not None else DEFAULT_MAX_FILE_MB,
    )

    STATE.scans[scan_id] = {
        "scan_id": scan_id,
        "command_id": command_id,
        "device_id": body.device_id,
        "roots": body.roots,
        "force": body.force,
        "status": "queued",
        "files_discovered": 0,
        "files_done": 0,
        "files_unscannable": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "findings": 0,
        "crawl_complete": False,
        "reject_reason": None,
        "error": None,
        "duration_ms": None,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }
    STATE.commands[command_id] = {
        "command_id": command_id,
        "type": "SCAN",
        "created_at": now,
        "device_id": body.device_id,
        "scan_id": scan_id,
        "payload": payload,
        "status": "pending",
        "delivered_at": None,
        "reject_reason": None,
    }
    return CreateScanResponse(scan_id=scan_id, command_id=command_id, status="queued")


@app.get("/admin/scans", response_model=list[ScanView])
def list_scans(device_id: Optional[str] = None, limit: int = 50, _auth: None = Depends(admin_auth)) -> list[ScanView]:
    scans = list(STATE.scans.values())
    if device_id is not None:
        scans = [s for s in scans if s["device_id"] == device_id]
    scans.sort(key=lambda s: s["created_at"], reverse=True)
    return [_scan_view(s) for s in scans[:limit]]


@app.get("/admin/scans/{scan_id}", response_model=ScanView)
def get_scan(scan_id: str, _auth: None = Depends(admin_auth)) -> ScanView:
    scan = STATE.scans.get(scan_id)
    if scan is None:
        api_error(404, "SCAN_NOT_FOUND", f"no scan {scan_id}")
    return _scan_view(scan)


@app.get("/admin/devices", response_model=list[DeviceView])
def list_devices(_auth: None = Depends(admin_auth)) -> list[DeviceView]:
    now = utcnow()
    out: list[DeviceView] = []
    for d in STATE.devices.values():
        last_seen = d.get("last_seen")
        online = last_seen is not None and (now - last_seen).total_seconds() <= 30
        out.append(
            DeviceView(
                device_id=d["device_id"],
                last_seen=iso(last_seen),
                files_scanned=d.get("files_scanned", 0),
                agent_version=d.get("agent_version"),
                online=online,
            )
        )
    return out


@app.get("/admin/findings", response_model=list[Finding])
def list_findings(
    device_id: Optional[str] = None,
    scan_id: Optional[str] = None,
    tier: Optional[str] = None,
    category: Optional[str] = None,
    type: Optional[str] = None,
    folder: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    _auth: None = Depends(admin_auth),
) -> list[Finding]:
    items = list(STATE.findings.values())
    if device_id is not None:
        items = [f for f in items if f.device_id == device_id]
    if scan_id is not None:
        items = [f for f in items if f.scan_id == scan_id]
    if tier is not None:
        items = [f for f in items if f.sensitivity_tier == tier]
    if category is not None:
        items = [f for f in items if f.category == category]
    if type is not None:
        items = [f for f in items if f.pii_type == type]
    if folder is not None:
        items = [f for f in items if f.folder_class == folder]
    items.sort(key=lambda f: f.risk_score, reverse=True)
    return items[offset : offset + limit]


@app.get("/admin/summary")
def admin_summary(_auth: None = Depends(admin_auth)) -> dict:
    findings = list(STATE.findings.values())

    by_tier = {"restricted": 0, "confidential": 0, "internal": 0, "public": 0}
    by_category: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_device: dict[str, int] = {}
    per_file: dict[tuple[str, str], dict] = {}

    for f in findings:
        by_tier[f.sensitivity_tier] = by_tier.get(f.sensitivity_tier, 0) + 1
        by_category[f.category] = by_category.get(f.category, 0) + 1
        if f.pii_type:
            by_type[f.pii_type] = by_type.get(f.pii_type, 0) + 1
        by_device[f.device_id] = by_device.get(f.device_id, 0) + 1

        key = (f.device_id, f.file_path)
        entry = per_file.setdefault(
            key,
            {
                "device_id": f.device_id,
                "file_path": f.file_path,
                "sensitivity_tier": f.sensitivity_tier,
                "max_risk_score": f.risk_score,
                "risk_band": f.risk_band,
                "findings": 0,
            },
        )
        entry["findings"] += 1
        if f.risk_score > entry["max_risk_score"]:
            entry["max_risk_score"] = f.risk_score
            entry["risk_band"] = f.risk_band
            entry["sensitivity_tier"] = f.sensitivity_tier

    top_risky_files = sorted(per_file.values(), key=lambda e: e["max_risk_score"], reverse=True)[:10]
    files_scanned = sum(d.get("files_scanned", 0) for d in STATE.devices.values())

    return {
        "devices": len(STATE.devices),
        "files_scanned": files_scanned,
        "findings_total": len(findings),
        "by_tier": by_tier,
        "by_category": by_category,
        "by_type": by_type,
        "by_device": by_device,
        "top_risky_files": top_risky_files,
    }


@app.get("/admin/pipeline-stats")
def pipeline_stats(_auth: None = Depends(admin_auth)) -> dict:
    decided_by_counts = {"rules": 0, "local_model": 0, "server": 0}
    for f in STATE.findings.values():
        decided_by_counts[f.decided_by] = decided_by_counts.get(f.decided_by, 0) + 1

    ocr_calls = sum(1 for t in STATE.traces if t.get("kind") == "ocr")

    return {
        "findings_by_decided_by": decided_by_counts,
        "llm_calls": {"verify": 0, "classify": STATE.detect_calls, "ocr": ocr_calls},
        "key_rotations": 0,
        "p50_latency_ms": {"verify": 0, "classify": 5, "ocr": 5},
        "est_cost_usd": 0.0,
    }


@app.post("/admin/reset", response_model=OkResponse)
def admin_reset(_auth: None = Depends(admin_auth)) -> OkResponse:
    STATE.reset()
    return OkResponse(ok=True)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
