"""Shared fixtures.

The corpus values here are fake but realistically shaped: the privacy tests
search the agent database for them, so they must look like the real thing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from agent.client import ServerClient            # noqa: E402
from agent.config import Config                  # noqa: E402
from agent.store import Store                    # noqa: E402

# Fake identifiers planted in the corpus (never real values).
AADHAAR_SPACED = "2345 6789 4821"
AADHAAR_PLAIN = "234567894821"
PAN = "ABCDE1234F"
MOBILE = "9876543210"
EMAIL = "ravi.kumar@example.in"
SECRETS = [AADHAAR_SPACED, AADHAAR_PLAIN, PAN, MOBILE, EMAIL]


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    """A Config pointed at a throwaway database, with no real server."""
    os.environ["AGENT_DB"] = str(tmp_path / "agent.db")
    os.environ["SERVER_URL"] = "http://testserver"
    os.environ["DEVICE_ID"] = "LAPTOP-TEST"
    os.environ["RETRY_ATTEMPTS"] = "2"
    os.environ["RETRY_BACKOFF_SEC"] = "0"
    os.environ.pop("DEVICE_TOKEN", None)
    return Config()


@pytest.fixture()
def store(config: Config) -> Store:
    store = Store(config.db_file)
    yield store
    store.close()


@pytest.fixture()
def mock_app(monkeypatch: pytest.MonkeyPatch):
    """A fresh in-process mock server (tools/mock_server.py)."""
    monkeypatch.setenv("MOCK_FINDINGS_PER_FILE", "1")
    monkeypatch.delenv("MOCK_OCR_FAIL", raising=False)
    monkeypatch.delenv("MOCK_FAIL_RATE", raising=False)
    monkeypatch.delenv("MOCK_FAIL_ONCE_ON", raising=False)
    import mock_server

    mock_server.STATE.reset()
    return mock_server.app


@pytest.fixture()
def client(config: Config, mock_app) -> ServerClient:
    from fastapi.testclient import TestClient

    http = TestClient(mock_app)
    server_client = ServerClient(config, client=http)
    yield server_client
    http.close()


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    """A small demo folder covering every Phase 1A file type."""
    root = tmp_path / "demo_folder"
    (root / "Documents").mkdir(parents=True)
    (root / "Downloads").mkdir(parents=True)
    (root / "node_modules" / "deep").mkdir(parents=True)
    (root / ".git").mkdir(parents=True)

    (root / "Documents" / "chat.txt").write_text(
        f"[12/09/26, 10:14] Ravi: bhai mera aadhar no hai {AADHAAR_SPACED}\n"
        f"PAN {PAN} mobile {MOBILE} email {EMAIL}\n",
        encoding="utf-8",
    )
    (root / "Documents" / "notes.md").write_text("# Notes\nnothing here\n", encoding="utf-8")
    (root / "Downloads" / "customers.csv").write_text(
        "name,email,mobile,aadhaar\n"
        + "".join(
            f"Cust {i},cust{i}@example.in,98765432{i:02d},2345678948{i:02d}\n" for i in range(60)
        ),
        encoding="utf-8",
    )
    (root / "ignored.bin").write_bytes(b"\x00binary")
    (root / "node_modules" / "deep" / "skip.txt").write_text("excluded", encoding="utf-8")
    (root / ".git" / "config.txt").write_text("excluded", encoding="utf-8")
    return root


def make_xlsx(path: Path, rows: int = 60, sheets: int = 1) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    for index in range(sheets):
        worksheet = workbook.active if index == 0 else workbook.create_sheet()
        worksheet.title = f"sheet{index + 1}"
        worksheet.append(["employee", "pan", "account"])
        for row in range(rows):
            worksheet.append([f"Emp {row}", PAN, f"5012345678{row:02d}"])
    workbook.save(path)
    return path


def make_docx(path: Path) -> Path:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Employee KYC record")
    document.add_paragraph(f"Aadhaar: {AADHAAR_SPACED}")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "PAN"
    table.cell(0, 1).text = PAN
    document.save(path)
    return path


def make_pdf(path: Path, text_pages: list[str]) -> Path:
    """Build a PDF whose pages carry exactly the given text (empty = 'scanned')."""
    canvas_module = pytest.importorskip("reportlab.pdfgen.canvas")
    pdf = canvas_module.Canvas(str(path))
    for text in text_pages:
        if text:
            pdf.drawString(72, 720, text)
        else:
            pdf.drawString(72, 720, ".")        # under the 20-char text threshold
        pdf.showPage()
    pdf.save()
    return path


def make_png(path: Path) -> Path:
    Image = pytest.importorskip("PIL.Image")
    image = Image.new("RGB", (240, 80), "white")
    image.save(path)
    return path
