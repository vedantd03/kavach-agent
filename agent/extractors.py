"""Turn a file into ``Chunk`` objects (api.md 7.3).

**Phase 1A rule 0: no detection, no ML and no OCR happen here.**  The agent only
parses documents and splits the text.  Images and text-less PDF pages are handed
to the caller's ``ocr`` callable, which posts them to the server's ``/ocr``.

Every extractor works in memory and returns text that the caller sends and then
drops; nothing here writes to disk or to the database.
"""

from __future__ import annotations

import csv
import io
import os
import zipfile
from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence

from detect_core.contracts import (
    CHUNK_OVERLAP_CHARS,
    IMAGE_TYPES,
    MAX_CHUNK_CHARS,
    MAX_CHUNKS_PER_FILE,
    MAX_OCR_IMAGE_BYTES,
    MAX_OCR_PAGES_PER_PDF,
    MAX_SHEET_ROWS,
    PDF_OCR_DPI,
    PDF_TEXT_MIN_CHARS,
    SHEET_ROW_BLOCK,
    SHEET_TYPES,
    TEXT_TYPES,
    Chunk,
    OcrResult,
    chunk_id_for,
)

from agent.util import get_logger, safe_error

log = get_logger("extract")

#: OLE compound-document magic.  A .docx/.xlsx that starts with this is an
#: encrypted Office file, not a zip container.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: Worst case for the whole-file reason, most important first.
_REASON_PRIORITY = ("truncated", "ocr_page_cap", "ocr_failed")


class Unscannable(Exception):
    """The file cannot be parsed at all: no chunks, ``status='unscannable'``."""

    def __init__(self, reason: str, cause: Optional[BaseException] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


class OcrCaller(Protocol):
    """Posts an image to the server's ``/ocr``; returns ``None`` if it failed."""

    def __call__(
        self,
        image_bytes: bytes,
        *,
        file_hash: str,
        page: Optional[int],
        filename: str,
        content_type: str,
    ) -> Optional[OcrResult]:
        ...


@dataclass
class FileFacts:
    """The identity of the file being extracted (copied onto every chunk)."""

    file_path: str
    file_hash: str
    file_type: str
    folder_class: str


@dataclass
class ExtractResult:
    chunks: list[Chunk] = field(default_factory=list)
    status: str = "ok"
    status_reason: Optional[str] = None
    ocr_pages: int = 0


# --------------------------------------------------------------------------- #
# chunk assembly
# --------------------------------------------------------------------------- #

def split_text(
    text: str,
    size: int = MAX_CHUNK_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """Windows of <= ``size`` characters with ``overlap`` characters of carry-over."""
    if not text or not text.strip():
        return []
    if len(text) <= size:
        return [text]
    step = max(1, size - overlap)
    pieces: list[str] = []
    start = 0
    while start < len(text):
        piece = text[start:start + size]
        if piece.strip():
            pieces.append(piece)
        if start + size >= len(text):
            break
        start += step
    return pieces


class ChunkBuilder:
    """Assigns chunk indices across the whole file and enforces the per-file cap."""

    def __init__(self, facts: FileFacts, cap: int = MAX_CHUNKS_PER_FILE) -> None:
        self.facts = facts
        self.cap = cap
        self.chunks: list[Chunk] = []
        self.truncated = False

    @property
    def full(self) -> bool:
        return len(self.chunks) >= self.cap

    def add(
        self,
        text: str,
        *,
        page: Optional[int] = None,
        row: Optional[int] = None,
        column: Optional[int] = None,
        column_header: Optional[str] = None,
        ocr_confidence: Optional[float] = None,
    ) -> bool:
        """Append one chunk.  Returns False once the per-file cap is reached."""
        if not text or not text.strip():
            return True
        if self.full:
            self.truncated = True
            return False
        facts = self.facts
        self.chunks.append(
            Chunk(
                chunk_id=chunk_id_for(facts.file_hash, len(self.chunks)),
                file_path=facts.file_path,
                file_hash=facts.file_hash,
                file_type=facts.file_type,
                folder_class=facts.folder_class,
                page=page,
                row=row,
                column=column,
                column_header=column_header,
                ocr_confidence=ocr_confidence,
                text=text,
            )
        )
        return True

    def add_text(
        self,
        text: str,
        *,
        page: Optional[int] = None,
        ocr_confidence: Optional[float] = None,
    ) -> bool:
        for piece in split_text(text):
            if not self.add(piece, page=page, ocr_confidence=ocr_confidence):
                return False
        return True


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def extract_file(
    path: str,
    facts: FileFacts,
    ocr: Optional[OcrCaller] = None,
) -> ExtractResult:
    """Extract and chunk one file.  Never raises for a parse problem."""
    builder = ChunkBuilder(facts)
    reasons: list[str] = []
    ocr_pages = 0

    try:
        file_type = facts.file_type
        if file_type in TEXT_TYPES:
            _extract_text_file(path, builder)
        elif file_type == "docx":
            _extract_docx(path, builder)
        elif file_type == "pdf":
            ocr_pages = _extract_pdf(path, facts, builder, ocr, reasons)
        elif file_type in SHEET_TYPES:
            _extract_sheet(path, facts, builder, reasons)
        elif file_type in IMAGE_TYPES:
            ocr_pages = _extract_image(path, facts, builder, ocr, reasons)
        else:
            # The crawler filters by include_types, so this is a defensive branch.
            _extract_text_file(path, builder)
    except Unscannable as exc:
        return ExtractResult([], "unscannable", exc.reason, ocr_pages)
    except PermissionError as exc:
        log.info("permission denied: %s", safe_error(exc))
        return ExtractResult([], "unscannable", "permission_denied", ocr_pages)
    except (OSError, MemoryError) as exc:
        log.info("read failed: %s", safe_error(exc))
        return ExtractResult([], "unscannable", "corrupt", ocr_pages)
    except Exception as exc:                                     # noqa: BLE001
        log.warning("extract failed for %s: %s", facts.file_type, safe_error(exc))
        return ExtractResult([], "unscannable", "extract_failed", ocr_pages)

    if builder.truncated:
        reasons.append("truncated")

    # An image or scanned PDF that produced nothing because OCR failed is
    # unscannable; a genuinely empty text file is merely a zero-chunk file.
    if not builder.chunks and "ocr_failed" in reasons:
        return ExtractResult([], "unscannable", "ocr_failed", ocr_pages)

    return ExtractResult(builder.chunks, "ok", _pick_reason(reasons), ocr_pages)


def _pick_reason(reasons: Sequence[str]) -> Optional[str]:
    for candidate in _REASON_PRIORITY:
        if candidate in reasons:
            return candidate
    return reasons[0] if reasons else None


# --------------------------------------------------------------------------- #
# plain text and .eml
# --------------------------------------------------------------------------- #

def _read_text(path: str) -> str:
    with open(path, "rb") as handle:
        raw = handle.read()
    return raw.decode("utf-8", errors="replace")


def _extract_text_file(path: str, builder: ChunkBuilder) -> None:
    builder.add_text(_read_text(path))


# --------------------------------------------------------------------------- #
# .docx
# --------------------------------------------------------------------------- #

def _extract_docx(path: str, builder: ChunkBuilder) -> None:
    _reject_encrypted_office(path)
    try:
        import docx                                              # python-docx
    except ImportError as exc:                                   # pragma: no cover
        raise Unscannable("unsupported", exc) from exc

    try:
        document = docx.Document(path)
    except zipfile.BadZipFile as exc:
        raise Unscannable("corrupt", exc) from exc
    except Exception as exc:                                     # noqa: BLE001
        raise Unscannable("corrupt", exc) from exc

    lines: list[str] = [para.text for para in document.paragraphs if para.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.replace("\n", " ").strip() for cell in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    builder.add_text("\n".join(lines))


def _reject_encrypted_office(path: str) -> None:
    """An OLE-wrapped .docx/.xlsx is password protected."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except PermissionError:
        raise
    except OSError as exc:
        raise Unscannable("corrupt", exc) from exc
    if head == _OLE_MAGIC:
        raise Unscannable("encrypted")


# --------------------------------------------------------------------------- #
# .pdf
# --------------------------------------------------------------------------- #

def _extract_pdf(
    path: str,
    facts: FileFacts,
    builder: ChunkBuilder,
    ocr: Optional[OcrCaller],
    reasons: list[str],
) -> int:
    try:
        import pdfplumber
    except ImportError as exc:                                   # pragma: no cover
        raise Unscannable("unsupported", exc) from exc

    from pdfminer.pdfdocument import PDFPasswordIncorrect        # type: ignore[import-untyped]

    ocr_pages = 0
    try:
        pdf = pdfplumber.open(path)
    except PDFPasswordIncorrect as exc:
        raise Unscannable("encrypted", exc) from exc
    except Exception as exc:                                     # noqa: BLE001
        if "password" in str(exc).lower() or "encrypt" in str(exc).lower():
            raise Unscannable("encrypted", exc) from exc
        raise Unscannable("corrupt", exc) from exc

    with pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            if builder.full:
                builder.truncated = True
                break
            try:
                text = page.extract_text() or ""
            except Exception as exc:                             # noqa: BLE001
                log.info("pdf page %d text failed: %s", page_number, safe_error(exc))
                text = ""

            if len(text.strip()) >= PDF_TEXT_MIN_CHARS:
                builder.add_text(text, page=page_number)
                continue

            # Scanned page: render and send to the server for OCR.
            if ocr is None:
                continue
            if ocr_pages >= MAX_OCR_PAGES_PER_PDF:
                if "ocr_page_cap" not in reasons:
                    reasons.append("ocr_page_cap")
                continue

            image_bytes = _render_pdf_page(page, page_number)
            if image_bytes is None:
                if "ocr_failed" not in reasons:
                    reasons.append("ocr_failed")
                continue

            ocr_pages += 1
            result = ocr(
                image_bytes,
                file_hash=facts.file_hash,
                page=page_number,
                filename=f"page-{page_number}.png",
                content_type="image/png",
            )
            if result is None:
                if "ocr_failed" not in reasons:
                    reasons.append("ocr_failed")
                continue
            builder.add_text(result.text, page=page_number, ocr_confidence=result.confidence)

    return ocr_pages


def _render_pdf_page(page: object, page_number: int) -> Optional[bytes]:
    """Render one page to PNG bytes at 150 dpi.  ``None`` if rendering failed."""
    try:
        rendered = page.to_image(resolution=PDF_OCR_DPI)         # type: ignore[attr-defined]
        buffer = io.BytesIO()
        rendered.original.convert("RGB").save(buffer, format="PNG", optimize=True)
        return _fit_image_limit(buffer.getvalue())[0]
    except Exception as exc:                                     # noqa: BLE001
        log.info("pdf page %d render failed: %s", page_number, safe_error(exc))
        return None


# --------------------------------------------------------------------------- #
# images
# --------------------------------------------------------------------------- #

def _extract_image(
    path: str,
    facts: FileFacts,
    builder: ChunkBuilder,
    ocr: Optional[OcrCaller],
    reasons: list[str],
) -> int:
    if ocr is None:
        return 0
    try:
        payload, content_type = _image_payload(path, facts.file_type)
    except Unscannable:
        raise
    except Exception as exc:                                     # noqa: BLE001
        raise Unscannable("corrupt", exc) from exc

    name = os.path.basename(path)
    suffix = "png" if content_type == "image/png" else "jpg"
    if not name.lower().endswith(f".{suffix}"):
        name = f"{os.path.splitext(name)[0]}.{suffix}"
    result = ocr(
        payload,
        file_hash=facts.file_hash,
        page=1,
        filename=name,
        content_type=content_type,
    )
    if result is None:
        reasons.append("ocr_failed")
        return 1
    builder.add_text(result.text, page=1, ocr_confidence=result.confidence)
    return 1


def _image_payload(path: str, file_type: str) -> tuple[bytes, str]:
    """Bytes the server will accept: PNG or JPEG, <= 10 MB.  TIFF -> first frame PNG."""
    with open(path, "rb") as handle:
        raw = handle.read()

    if file_type in {"png", "jpg", "jpeg"} and len(raw) <= MAX_OCR_IMAGE_BYTES:
        return raw, "image/png" if file_type == "png" else "image/jpeg"

    from PIL import Image                                        # Pillow: handling only

    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.seek(0)                                        # TIFF: first frame
            frame = image.convert("RGB")
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG", optimize=True)
    except Exception as exc:                                     # noqa: BLE001
        raise Unscannable("corrupt", exc) from exc
    return _fit_image_limit(buffer.getvalue())


def _fit_image_limit(png_bytes: bytes, limit: int = MAX_OCR_IMAGE_BYTES) -> tuple[bytes, str]:
    """Shrink until the payload fits the server's 10 MB /ocr limit."""
    if len(png_bytes) <= limit:
        return png_bytes, "image/png"

    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as image:
        frame = image.convert("RGB")
        for quality in (85, 70, 55):
            buffer = io.BytesIO()
            frame.save(buffer, format="JPEG", quality=quality, optimize=True)
            if buffer.tell() <= limit:
                return buffer.getvalue(), "image/jpeg"
        scaled = frame
        for _ in range(4):
            scaled = scaled.resize((max(1, scaled.width // 2), max(1, scaled.height // 2)))
            buffer = io.BytesIO()
            scaled.save(buffer, format="JPEG", quality=70, optimize=True)
            if buffer.tell() <= limit:
                return buffer.getvalue(), "image/jpeg"
    raise Unscannable("oversize")


# --------------------------------------------------------------------------- #
# .csv / .tsv / .xlsx  -> column chunks
# --------------------------------------------------------------------------- #

def _extract_sheet(
    path: str,
    facts: FileFacts,
    builder: ChunkBuilder,
    reasons: list[str],
) -> None:
    if facts.file_type == "xlsx":
        sheets = _read_xlsx(path)
    else:
        sheets = [(1, _read_delimited(path, "\t" if facts.file_type == "tsv" else ","))]

    for sheet_index, rows in sheets:
        if not rows:
            continue
        header = [str(cell) if cell is not None else "" for cell in rows[0]]
        data_rows = rows[1:]
        if len(data_rows) > MAX_SHEET_ROWS:
            data_rows = data_rows[:MAX_SHEET_ROWS]
            if "truncated" not in reasons:
                reasons.append("truncated")
        if not _emit_column_chunks(builder, header, data_rows, sheet_index):
            break


def _emit_column_chunks(
    builder: ChunkBuilder,
    header: Sequence[str],
    data_rows: Sequence[Sequence[object]],
    sheet_index: int,
) -> bool:
    """One chunk per (50-row block x column).  Returns False if the cap was hit.

    ``row`` is the spreadsheet row number of the chunk's first line, so the
    server can map line ``k`` of the chunk to row ``chunk.row + k``.  A block
    whose text would exceed the chunk limit is split by lines, keeping that
    mapping intact.
    """
    width = max([len(header)] + [len(row) for row in data_rows]) if data_rows else len(header)

    for block_start in range(0, len(data_rows), SHEET_ROW_BLOCK):
        block = data_rows[block_start:block_start + SHEET_ROW_BLOCK]
        first_row_number = block_start + 2                       # row 1 is the header
        for column_index in range(width):
            lines = [_cell_text(row[column_index]) if column_index < len(row) else ""
                     for row in block]
            if not any(line.strip() for line in lines):
                continue
            column_header = header[column_index] if column_index < len(header) else ""
            for offset, piece in _pack_lines(lines):
                ok = builder.add(
                    piece,
                    page=sheet_index,
                    row=first_row_number + offset,
                    column=column_index + 1,
                    column_header=column_header or None,
                )
                if not ok:
                    return False
    return True


def _pack_lines(lines: Sequence[str]) -> list[tuple[int, str]]:
    """Group lines into <= MAX_CHUNK_CHARS pieces, keeping the line offset of each."""
    pieces: list[tuple[int, str]] = []
    current: list[str] = []
    current_start = 0
    length = 0
    for index, line in enumerate(lines):
        addition = len(line) + 1
        if current and length + addition > MAX_CHUNK_CHARS:
            pieces.append((current_start, "\n".join(current)))
            current, length, current_start = [], 0, index
        if addition > MAX_CHUNK_CHARS:                           # single oversized cell
            for part in split_text(line):
                pieces.append((index, part))
            current, length, current_start = [], 0, index + 1
            continue
        current.append(line)
        length += addition
    if current:
        pieces.append((current_start, "\n".join(current)))
    return pieces


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\r", " ").replace("\n", " ")


def _read_delimited(path: str, delimiter: str) -> list[list[str]]:
    text = _read_text(path)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows: list[list[str]] = []
    try:
        for row in reader:
            rows.append(row)
            if len(rows) > MAX_SHEET_ROWS + 1:
                break
    except csv.Error as exc:
        if not rows:
            raise Unscannable("corrupt", exc) from exc
        log.info("csv stopped early: %s", safe_error(exc))
    return rows


def _read_xlsx(path: str) -> list[tuple[int, list[list[object]]]]:
    _reject_encrypted_office(path)
    try:
        import openpyxl
    except ImportError as exc:                                   # pragma: no cover
        raise Unscannable("unsupported", exc) from exc

    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except zipfile.BadZipFile as exc:
        raise Unscannable("corrupt", exc) from exc
    except Exception as exc:                                     # noqa: BLE001
        raise Unscannable("corrupt", exc) from exc

    sheets: list[tuple[int, list[list[object]]]] = []
    try:
        for sheet_index, worksheet in enumerate(workbook.worksheets, start=1):
            rows: list[list[object]] = []
            for row in worksheet.iter_rows(values_only=True):
                rows.append(list(row))
                if len(rows) > MAX_SHEET_ROWS + 1:
                    break
            sheets.append((sheet_index, rows))
    finally:
        workbook.close()
    return sheets
