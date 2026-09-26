"""HTTP client for the server API (api.md 3).

One method per endpoint, each returning a parsed contract model.  Retry policy
(api.md 1): network errors and 429/502/503/504 are retried with backoff
1, 2, 4, 8, 16 s; any other 4xx fails immediately and is not retried.

Privacy: this module is the only place chunk text and image bytes leave the
process, and it never logs a request or response body.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable, Optional, TypeVar

import httpx

from detect_core.contracts import (
    Command,
    CommandAck,
    DetectRequest,
    DetectResponse,
    HeartbeatRequest,
    OcrResult,
    ScanComplete,
    ScanProgress,
)

from agent.config import Config
from agent.util import get_logger

log = get_logger("client")

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class ServerError(RuntimeError):
    """A non-retryable server answer, or retries exhausted."""

    def __init__(self, message: str, status_code: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.status_code is None or self.status_code in RETRYABLE_STATUS


class ScanClosed(ServerError):
    """409 from /progress: the server considers the scan finished."""


class NotFound(ServerError):
    """404 COMMAND_NOT_FOUND / SCAN_NOT_FOUND."""


class ServerClient:
    """Thread-safe: ``httpx.Client`` is safe to share across threads."""

    def __init__(self, config: Config, client: Optional[httpx.Client] = None) -> None:
        self.config = config
        headers = {"User-Agent": f"kavach-agent/{config.agent_version}"}
        if config.device_token:
            headers["X-Device-Token"] = config.device_token
        self._client = client or httpx.Client(
            base_url=config.server_url,
            timeout=httpx.Timeout(config.timeout_default_sec),
            follow_redirects=False,
        )
        self._client.headers.update(headers)     # also for an injected client (tests)
        self._owns_client = client is None
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ServerClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # retry wrapper
    # ------------------------------------------------------------------ #

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        json_body: Any = None,
        files: Any = None,
        data: Any = None,
        params: Any = None,
    ) -> httpx.Response:
        attempts = max(1, self.config.retry_attempts)
        backoffs = self.config.retry_backoff_sec or [1.0, 2.0, 4.0, 8.0, 16.0]
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                response = self._client.request(
                    method, path, json=json_body, files=files, data=data,
                    params=params, timeout=timeout,
                )
            except httpx.HTTPError as exc:                       # network / timeout
                last_error = ServerError(f"{type(exc).__name__} calling {method} {path}")
                log.warning("%s %s failed (%s), attempt %d/%d",
                            method, path, type(exc).__name__, attempt + 1, attempts)
            else:
                if response.status_code < 400:
                    return response
                error = _error_for(response, method, path)
                if not error.retryable:
                    raise error
                last_error = error
                log.warning("%s %s -> %d, attempt %d/%d",
                            method, path, response.status_code, attempt + 1, attempts)

            if attempt < attempts - 1:
                delay = backoffs[min(attempt, len(backoffs) - 1)]
                time.sleep(delay + random.uniform(0, 0.25 * delay))    # jitter

        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------ #
    # endpoints
    # ------------------------------------------------------------------ #

    def health(self) -> dict[str, Any]:
        response = self._request("GET", "/health", timeout=self.config.timeout_default_sec)
        return dict(response.json())

    def next_command(self, device_id: Optional[str] = None) -> Optional[Command]:
        """``None`` when the server answers 204 (nothing pending)."""
        device = device_id or self.config.device_id
        response = self._request(
            "GET", f"/devices/{device}/commands/next", timeout=self.config.timeout_poll_sec
        )
        if response.status_code == 204 or not response.content:
            return None
        return Command.model_validate(response.json())

    def ack_command(self, command_id: str, status: str, reason: Optional[str] = None) -> None:
        ack = CommandAck(status=status, reason=reason)          # type: ignore[arg-type]
        self._request(
            "POST", f"/commands/{command_id}/ack",
            json_body=ack.model_dump(), timeout=self.config.timeout_default_sec,
        )

    def ocr(
        self,
        image_bytes: bytes,
        *,
        file_hash: str,
        page: Optional[int] = None,
        filename: str = "page.png",
        content_type: str = "image/png",
    ) -> OcrResult:
        files = {"image": (filename, image_bytes, content_type)}
        data: dict[str, str] = {"device_id": self.config.device_id, "file_hash": file_hash}
        if page is not None:
            data["page"] = str(page)
        response = self._request(
            "POST", "/ocr", files=files, data=data, timeout=self.config.timeout_ocr_sec
        )
        return OcrResult.model_validate(response.json())

    def detect(self, request: DetectRequest) -> DetectResponse:
        response = self._request(
            "POST", "/detect",
            json_body=request.model_dump(mode="json"),
            timeout=self.config.timeout_detect_sec,
        )
        return DetectResponse.model_validate(response.json())

    def progress(self, scan_id: str, progress: ScanProgress) -> None:
        self._request(
            "POST", f"/scans/{scan_id}/progress",
            json_body=progress.model_dump(mode="json"),
            timeout=self.config.timeout_default_sec,
        )

    def complete(self, scan_id: str, payload: ScanComplete) -> None:
        self._request(
            "POST", f"/scans/{scan_id}/complete",
            json_body=payload.model_dump(mode="json"),
            timeout=self.config.timeout_default_sec,
        )

    def heartbeat(self, files_scanned: int) -> None:
        payload = HeartbeatRequest(
            device_id=self.config.device_id,
            files_scanned=files_scanned,
            agent_version=self.config.agent_version,
        )
        self._request(
            "POST", "/heartbeat",
            json_body=payload.model_dump(mode="json"),
            timeout=self.config.timeout_default_sec,
        )


def _error_for(response: httpx.Response, method: str, path: str) -> ServerError:
    """Build a typed error.  The body is parsed for its code only, never logged."""
    code: Optional[str] = None
    message = ""
    try:
        body = response.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            code = detail.get("code")
            message = str(detail.get("message", ""))[:200]
        elif isinstance(detail, (str, list)):
            message = str(detail)[:200]
    except Exception:                                            # noqa: BLE001 - body may be empty/HTML
        message = ""

    text = f"{method} {path} -> {response.status_code}"
    if code:
        text += f" {code}"
    if message:
        text += f": {message}"

    if response.status_code == 409:
        return ScanClosed(text, response.status_code, code)
    if response.status_code == 404:
        return NotFound(text, response.status_code, code)
    return ServerError(text, response.status_code, code)


def with_retry_result(fn: Callable[[], T], default: T, what: str) -> T:
    """Run ``fn``; on a ServerError log it and return ``default`` (used by loops)."""
    try:
        return fn()
    except ServerError as exc:
        log.warning("%s failed: %s", what, exc)
        return default
