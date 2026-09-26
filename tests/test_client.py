"""HTTP behaviour from api.md 1: retries, backoff, timeouts, typed errors."""

from __future__ import annotations

import httpx
import pytest

from detect_core.contracts import DetectRequest, ScanProgress

from agent.client import NotFound, ScanClosed, ServerClient, ServerError
from agent.config import Config


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr("agent.client.time.sleep", lambda seconds: slept.append(seconds))
    return slept


def client_with(config: Config, handler) -> ServerClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="http://testserver")
    return ServerClient(config, client=http)


def test_retries_retryable_status_then_succeeds(config: Config, no_sleep: list[float]):
    config = Config(retry_attempts=5, retry_backoff_sec=[1, 2, 4, 8, 16])
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, json={"detail": {"code": "OCR_UNAVAILABLE", "message": "x"}})
        return httpx.Response(200, json={"status": "ok"})

    assert client_with(config, handler).health() == {"status": "ok"}
    assert len(calls) == 3
    assert [round(s) for s in no_sleep] == [1, 2]            # backoff 1 s then 2 s


def test_gives_up_after_the_configured_attempts(config: Config, no_sleep: list[float]):
    config = Config(retry_attempts=3, retry_backoff_sec=[1, 2, 4])
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(502)

    with pytest.raises(ServerError) as info:
        client_with(config, handler).health()
    assert len(calls) == 3
    assert info.value.status_code == 502
    assert info.value.retryable


def test_does_not_retry_other_4xx(config: Config, no_sleep: list[float]):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(413, json={"detail": {"code": "BATCH_TOO_LARGE", "message": "too big"}})

    with pytest.raises(ServerError) as info:
        client_with(config, handler).detect(DetectRequest(device_id="LAPTOP-TEST"))
    assert len(calls) == 1
    assert info.value.code == "BATCH_TOO_LARGE" and not info.value.retryable
    assert no_sleep == []


def test_network_errors_are_retried(config: Config, no_sleep: list[float]):
    config = Config(retry_attempts=2, retry_backoff_sec=[1])
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ServerError):
        client_with(config, handler).heartbeat(5)
    assert len(calls) == 2


def test_204_means_no_command(config: Config):
    client = client_with(config, lambda request: httpx.Response(204))
    assert client.next_command() is None


def test_command_is_parsed_from_the_contract(config: Config):
    payload = {
        "command_id": "cmd_3f9a1c2b7d4e", "type": "SCAN",
        "created_at": "2026-09-26T09:30:00Z",
        "payload": {"scan_id": "scan_8b21e0d4a911", "roots": ["/tmp/demo"], "force": False,
                    "include_types": ["txt"], "exclude_dirs": [".git"], "max_file_mb": 50},
    }
    client = client_with(config, lambda request: httpx.Response(200, json=payload))
    command = client.next_command()
    assert command is not None
    assert command.command_id == "cmd_3f9a1c2b7d4e"
    assert command.payload.roots == ["/tmp/demo"]


def test_409_on_progress_is_scan_closed(config: Config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": {"code": "SCAN_CLOSED", "message": "closed"}})

    with pytest.raises(ScanClosed):
        client_with(config, handler).progress("scan_1", ScanProgress())


def test_404_is_not_found(config: Config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": {"code": "COMMAND_NOT_FOUND", "message": "no"}})

    with pytest.raises(NotFound):
        client_with(config, handler).ack_command("cmd_x", "accepted")


def test_device_token_header_is_sent_when_configured(monkeypatch: pytest.MonkeyPatch, config: Config):
    monkeypatch.setenv("DEVICE_TOKEN", "dev-token-1")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"status": "ok"})

    client_with(Config(), handler).health()
    assert seen["x-device-token"] == "dev-token-1"


def test_per_endpoint_timeouts(config: Config):
    seen: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout", {}).get("read"))
        return httpx.Response(200, json={"findings": [], "pending": 0, "stats": {}})

    client = client_with(Config(), handler)
    client.detect(DetectRequest(device_id="LAPTOP-TEST"))
    assert seen == [180.0]                                   # /detect gets 180 s


def test_ocr_posts_multipart_with_form_fields(config: Config):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        captured["content_type"] = request.headers["content-type"]
        captured["has_device"] = b'name="device_id"' in body
        captured["has_hash"] = b'name="file_hash"' in body
        captured["has_page"] = b'name="page"' in body
        captured["has_image"] = b'name="image"' in body
        return httpx.Response(200, json={"text": "hello", "confidence": 0.8, "pages": 1})

    result = client_with(Config(), handler).ocr(b"\x89PNG fake", file_hash="abc", page=2)
    assert result.text == "hello"
    assert captured["content_type"].startswith("multipart/form-data")
    assert all(captured[key] for key in ("has_device", "has_hash", "has_page", "has_image"))
