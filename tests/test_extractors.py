"""Chunking rules from api.md 7.3."""

from __future__ import annotations

from pathlib import Path

import pytest

from detect_core.contracts import (
    CHUNK_OVERLAP_CHARS,
    MAX_CHUNK_CHARS,
    MAX_OCR_PAGES_PER_PDF,
    OcrResult,
)

from agent.extractors import FileFacts, extract_file, split_text
from tests.conftest import AADHAAR_SPACED, PAN, make_docx, make_pdf, make_png, make_xlsx


def facts_for(path: Path, file_type: str) -> FileFacts:
    return FileFacts(str(path), "a" * 64, file_type, "documents")


class RecordingOcr:
    """Stands in for POST /ocr; records the calls the extractor would make."""

    def __init__(self, text: str = "ocr text from the server", fail: bool = False) -> None:
        self.text, self.fail = text, fail
        self.calls: list[dict[str, object]] = []

    def __call__(self, image_bytes, *, file_hash, page, filename, content_type):
        self.calls.append(
            {"bytes": len(image_bytes), "page": page, "name": filename, "type": content_type}
        )
        if self.fail:
            return None
        return OcrResult(text=self.text, confidence=0.82, pages=1)


# --------------------------------------------------------------------------- #
# split_text
# --------------------------------------------------------------------------- #

def test_split_text_respects_size_and_overlap():
    text = "".join(str(i % 10) for i in range(10_000))
    pieces = split_text(text)
    assert all(len(piece) <= MAX_CHUNK_CHARS for piece in pieces)
    assert pieces[0] == text[:MAX_CHUNK_CHARS]
    # the second window starts 150 characters before the end of the first
    assert pieces[1].startswith(text[MAX_CHUNK_CHARS - CHUNK_OVERLAP_CHARS:][:50])
    assert "".join(dict.fromkeys(pieces))                    # no empty pieces


def test_split_text_ignores_blank_input():
    assert split_text("") == []
    assert split_text("   \n\t ") == []


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #

def test_text_file_chunks_have_no_page_or_row(tmp_path: Path):
    path = tmp_path / "chat.txt"
    path.write_text(f"mera aadhar {AADHAAR_SPACED}\n" * 400, encoding="utf-8")
    result = extract_file(str(path), facts_for(path, "txt"))
    assert result.status == "ok"
    assert len(result.chunks) > 1
    assert all(chunk.page is None and chunk.row is None for chunk in result.chunks)
    assert [chunk.chunk_id for chunk in result.chunks] == [
        f"{'a' * 16}:{i}" for i in range(len(result.chunks))
    ]


def test_undecodable_bytes_do_not_raise(tmp_path: Path):
    path = tmp_path / "weird.log"
    path.write_bytes(b"\xff\xfe broken utf8 \x80\x81 aadhaar 2345 6789 4821")
    result = extract_file(str(path), facts_for(path, "log"))
    assert result.status == "ok" and result.chunks


def test_empty_file_is_ok_with_no_chunks(tmp_path: Path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    result = extract_file(str(path), facts_for(path, "txt"))
    assert result.status == "ok" and result.chunks == []


def test_per_file_chunk_cap_marks_truncated(tmp_path: Path):
    path = tmp_path / "huge.txt"
    path.write_text("x" * (MAX_CHUNK_CHARS * 1_100), encoding="utf-8")
    result = extract_file(str(path), facts_for(path, "txt"))
    assert len(result.chunks) == 1_000
    assert result.status_reason == "truncated"


# --------------------------------------------------------------------------- #
# spreadsheets
# --------------------------------------------------------------------------- #

def test_csv_produces_column_chunks_with_row_numbers(tmp_path: Path):
    path = tmp_path / "customers.csv"
    path.write_text(
        "name,email\n" + "".join(f"Cust {i},cust{i}@example.in\n" for i in range(120)),
        encoding="utf-8",
    )
    result = extract_file(str(path), facts_for(path, "csv"))
    assert result.status == "ok"
    # 120 data rows -> 3 blocks of 50 x 2 columns
    assert len(result.chunks) == 6
    first = result.chunks[0]
    assert (first.page, first.row, first.column, first.column_header) == (1, 2, 1, "name")
    assert first.text.splitlines()[0] == "Cust 0"
    assert [chunk.row for chunk in result.chunks] == [2, 2, 52, 52, 102, 102]
    assert [chunk.column for chunk in result.chunks] == [1, 2, 1, 2, 1, 2]
    emails = result.chunks[1]
    assert emails.column_header == "email"
    assert emails.text.splitlines()[3] == "cust3@example.in"


def test_csv_empty_column_is_skipped(tmp_path: Path):
    path = tmp_path / "sparse.csv"
    path.write_text("a,b,c\n1,,3\n4,,6\n", encoding="utf-8")
    result = extract_file(str(path), facts_for(path, "csv"))
    assert [chunk.column_header for chunk in result.chunks] == ["a", "c"]


def test_tsv_uses_tab_delimiter(tmp_path: Path):
    path = tmp_path / "rows.tsv"
    path.write_text("name\tpan\nRavi\t" + PAN + "\n", encoding="utf-8")
    result = extract_file(str(path), facts_for(path, "tsv"))
    assert [chunk.column_header for chunk in result.chunks] == ["name", "pan"]
    assert result.chunks[1].text == PAN


def test_xlsx_sheet_index_is_the_page(tmp_path: Path):
    path = make_xlsx(tmp_path / "payroll.xlsx", rows=60, sheets=2)
    result = extract_file(str(path), facts_for(path, "xlsx"))
    pages = {chunk.page for chunk in result.chunks}
    assert pages == {1, 2}
    sheet1 = [chunk for chunk in result.chunks if chunk.page == 1]
    assert [chunk.row for chunk in sheet1[:3]] == [2, 2, 2]
    assert [chunk.column for chunk in sheet1[:3]] == [1, 2, 3]
    assert sheet1[1].column_header == "pan"


def test_sheet_row_cap_marks_truncated(tmp_path: Path):
    path = tmp_path / "big.csv"
    path.write_text("id\n" + "".join(f"{i}\n" for i in range(5_200)), encoding="utf-8")
    result = extract_file(str(path), facts_for(path, "csv"))
    assert result.status_reason == "truncated"
    lines = sum(len(chunk.text.splitlines()) for chunk in result.chunks)
    assert lines == 5_000


def test_long_column_block_keeps_row_mapping(tmp_path: Path):
    """A block split for size must still let the server compute row + line."""
    cell = "y" * 500
    path = tmp_path / "wide.csv"
    path.write_text("note\n" + "".join(f"{cell}\n" for _ in range(50)), encoding="utf-8")
    result = extract_file(str(path), facts_for(path, "csv"))
    assert len(result.chunks) > 1
    assert result.chunks[0].row == 2
    # the second piece starts where the first one stopped
    consumed = len(result.chunks[0].text.splitlines())
    assert result.chunks[1].row == 2 + consumed


# --------------------------------------------------------------------------- #
# docx
# --------------------------------------------------------------------------- #

def test_docx_includes_paragraphs_and_table_rows(tmp_path: Path):
    path = make_docx(tmp_path / "kyc.docx")
    result = extract_file(str(path), facts_for(path, "docx"))
    text = "\n".join(chunk.text for chunk in result.chunks)
    assert "Employee KYC record" in text
    assert f"PAN | {PAN}" in text
    assert all(chunk.page is None for chunk in result.chunks)


def test_encrypted_office_file_is_unscannable(tmp_path: Path):
    path = tmp_path / "locked.docx"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    result = extract_file(str(path), facts_for(path, "docx"))
    assert (result.status, result.status_reason, result.chunks) == ("unscannable", "encrypted", [])


def test_corrupt_docx_is_unscannable(tmp_path: Path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"PK\x03\x04 not really a docx")
    result = extract_file(str(path), facts_for(path, "docx"))
    assert result.status == "unscannable"
    assert result.status_reason in {"corrupt", "extract_failed"}


# --------------------------------------------------------------------------- #
# pdf
# --------------------------------------------------------------------------- #

def test_pdf_text_pages_carry_page_numbers(tmp_path: Path):
    path = make_pdf(
        tmp_path / "invoice.pdf",
        ["Invoice 2026-0912 for Acme Pvt Ltd", "Page two also carries real extractable text"],
    )
    ocr = RecordingOcr()
    result = extract_file(str(path), facts_for(path, "pdf"), ocr=ocr)
    assert [chunk.page for chunk in result.chunks] == [1, 2]
    assert ocr.calls == []                                   # text pages never go to /ocr


def test_pdf_scanned_page_goes_to_ocr(tmp_path: Path):
    path = make_pdf(tmp_path / "scan.pdf", ["A page with plenty of real text on it", ""])
    ocr = RecordingOcr()
    result = extract_file(str(path), facts_for(path, "pdf"), ocr=ocr)
    assert len(ocr.calls) == 1 and ocr.calls[0]["page"] == 2
    assert ocr.calls[0]["type"] == "image/png"
    scanned = [chunk for chunk in result.chunks if chunk.page == 2]
    assert scanned and scanned[0].ocr_confidence == 0.82


def test_pdf_ocr_page_cap(tmp_path: Path):
    path = make_pdf(tmp_path / "many.pdf", [""] * (MAX_OCR_PAGES_PER_PDF + 3))
    ocr = RecordingOcr()
    result = extract_file(str(path), facts_for(path, "pdf"), ocr=ocr)
    assert len(ocr.calls) == MAX_OCR_PAGES_PER_PDF
    assert result.status_reason == "ocr_page_cap"


def test_pdf_keeps_text_pages_when_ocr_fails(tmp_path: Path):
    path = make_pdf(tmp_path / "mixed.pdf", ["Real text on the first page here", ""])
    result = extract_file(str(path), facts_for(path, "pdf"), ocr=RecordingOcr(fail=True))
    assert result.status == "ok"
    assert [chunk.page for chunk in result.chunks] == [1]
    assert result.status_reason == "ocr_failed"


def test_encrypted_pdf_is_unscannable(tmp_path: Path):
    path = tmp_path / "locked.pdf"
    path.write_bytes(b"%PDF-1.4\nnot a real pdf\n%%EOF\n")
    result = extract_file(str(path), facts_for(path, "pdf"), ocr=RecordingOcr())
    assert result.status == "unscannable"


# --------------------------------------------------------------------------- #
# images
# --------------------------------------------------------------------------- #

def test_png_is_sent_to_ocr_once(tmp_path: Path):
    path = make_png(tmp_path / "kyc_scan.png")
    ocr = RecordingOcr()
    result = extract_file(str(path), facts_for(path, "png"), ocr=ocr)
    assert len(ocr.calls) == 1
    assert ocr.calls[0]["type"] == "image/png" and ocr.calls[0]["page"] == 1
    assert result.chunks[0].page == 1 and result.chunks[0].ocr_confidence == 0.82


def test_image_with_failed_ocr_is_unscannable(tmp_path: Path):
    path = make_png(tmp_path / "kyc_scan.png")
    result = extract_file(str(path), facts_for(path, "png"), ocr=RecordingOcr(fail=True))
    assert (result.status, result.status_reason) == ("unscannable", "ocr_failed")


def test_tiff_is_converted_to_png(tmp_path: Path):
    Image = pytest.importorskip("PIL.Image")
    path = tmp_path / "scan.tiff"
    Image.new("RGB", (120, 60), "white").save(path)
    ocr = RecordingOcr()
    extract_file(str(path), facts_for(path, "tiff"), ocr=ocr)
    assert ocr.calls[0]["type"] == "image/png"
    assert ocr.calls[0]["name"].endswith(".png")


def test_image_without_ocr_callable_produces_nothing(tmp_path: Path):
    path = make_png(tmp_path / "x.png")
    result = extract_file(str(path), facts_for(path, "png"), ocr=None)
    assert result.status == "ok" and result.chunks == []
