"""Safe, real file type from bytes (spec 5.1).

The file name never chooses the family; it only picks md over txt.
Every check here reads container structure, never content.
"""

import csv
import io
import struct
import unicodedata
import warnings
import zipfile
import zlib
from collections import Counter

import olefile
from PIL import Image

from .caps import MB, SafetyCaps
from .textdecode import decode_text

ACCEPTED_KINDS = ("pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "odt", "odp", "ods",
                  "rtf", "txt", "md", "csv", "html", "image")  # fmt: skip

MESSAGES = {
    "empty_file": "The file is empty. Choose the file again and upload it.",
    "too_large": "This file is too large. Upload a smaller file or split it.",
    "unsupported_type": "This file type isn't supported. Upload a PDF, Office or "
    "OpenDocument file, RTF, text, CSV, HTML or an image.",
    "damaged_file": "This file is damaged and can't be read. Try saving or exporting it again.",
    "encrypted": "This file is password-protected. Remove the password and upload it again.",
    "macros": "Files with macros aren't accepted. Save it without macros (for example as .docx or PDF).",
    "zip_bomb": "This file expands to far more than its size, so it can't be processed safely.",
    "too_many_pages": "This file has too many pages. Split it into smaller parts.",
    "pdf_javascript": "PDFs with JavaScript aren't accepted. Print or export it again as a plain PDF.",
    "image_too_large": "This image is too large. Resize it below 50 megapixels and upload it again.",
    "poor_quality": "The uploaded file is not good enough quality. Upload a clearer copy.",
}

ODF_KINDS = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
}
SAMPLE_BYTES = 8192
CHUNK = 64 * 1024
MAX_ENTRY_RATIO = 1000


class FileRefused(Exception):
    def __init__(self, code: str, message: str | None = None):
        message = message or MESSAGES.get(code, code)
        super().__init__(code, message)
        self.code = code  # stable, returned to the client
        self.message = message  # user-facing, says what to do

    def __reduce__(self):  # must survive pickling back from the inspection child
        return (FileRefused, (self.code, self.message))


def sniff(data: bytes, caps: SafetyCaps, filename_hint: str | None = None) -> str:
    if not data:
        raise FileRefused("empty_file")
    if len(data) > caps.max_file_mb * MB:
        raise FileRefused("too_large")
    if data.startswith(b"%PDF-"):
        return "pdf"
    if _is_image(data):
        _check_image(data, caps)
        return "image"
    if data.startswith(b"PK\x03\x04"):
        return _sniff_zip(data, caps)
    if data.startswith(olefile.MAGIC):
        return _sniff_ole(data, caps)
    if data.startswith(b"{\\rtf"):
        return "rtf"
    return _sniff_text(data, filename_hint)


# ---- images -------------------------------------------------------------


def _is_image(data: bytes) -> bool:
    return data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"II*\x00", b"MM\x00*")) or (
        data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    )


def _check_image(data: bytes, caps: SafetyCaps) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:  # lazy: reads the header only
                width, height = img.size
    except Image.DecompressionBombError:
        raise FileRefused("image_too_large") from None
    except Exception:
        raise FileRefused("damaged_file") from None
    if width * height > caps.max_image_megapixels * 1_000_000:
        raise FileRefused("image_too_large")


# ---- ZIP containers -------------------------------------------------------


def open_zip(data: bytes, caps: SafetyCaps) -> zipfile.ZipFile:
    """Open a ZIP and run the zip-bomb checks: declared sizes, then real ones."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except (zipfile.BadZipFile, ValueError, OSError, EOFError):
        raise FileRefused("damaged_file") from None
    declared = sum(i.file_size for i in infos)
    if (
        len(infos) > caps.max_zip_entries
        or declared > caps.max_unpacked_mb * MB
        or declared / len(data) > caps.max_unpacked_ratio
        or any(i.file_size / max(i.compress_size, 1) > MAX_ENTRY_RATIO for i in infos)
    ):
        raise FileRefused("zip_bomb")
    _verify_real_sizes(data, z, infos, caps)
    return z


def _verify_real_sizes(data, z, infos, caps) -> None:
    """A hostile ZIP can lie in its directory, so decompress every entry once,
    counting real bytes and discarding them."""
    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            raise FileRefused("encrypted")
        limit = min(info.file_size, caps.max_entry_mb * MB)
        size = 0
        for chunk in _entry_chunks(data, z, info):
            size += len(chunk)
            total += len(chunk)
            if size > limit or total > caps.max_unpacked_mb * MB:
                raise FileRefused("zip_bomb")


def _entry_chunks(data, z, info):
    """Yields the entry's real decompressed bytes. zipfile itself stops at the
    declared size, which would hide a lie, so deflate is done here."""
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        try:
            with z.open(info) as f:
                while chunk := f.read(CHUNK):
                    yield chunk
        except (zipfile.BadZipFile, NotImplementedError, ValueError, EOFError, OSError, zlib.error):
            raise FileRefused("damaged_file") from None
        return
    header = data[info.header_offset : info.header_offset + 30]
    if len(header) < 30 or header[:4] != b"PK\x03\x04":
        raise FileRefused("damaged_file")
    name_len, extra_len = struct.unpack("<HH", header[26:30])
    start = info.header_offset + 30 + name_len + extra_len
    raw = memoryview(data)[start : start + info.compress_size]
    if info.compress_type == zipfile.ZIP_STORED:
        for i in range(0, len(raw), CHUNK):
            yield raw[i : i + CHUNK]
        return
    d = zlib.decompressobj(-15)
    try:
        for i in range(0, len(raw), CHUNK):
            buf = raw[i : i + CHUNK]
            while buf:
                out = d.decompress(buf, CHUNK)
                buf = d.unconsumed_tail
                if out:
                    yield out
    except zlib.error:
        raise FileRefused("damaged_file") from None


def read_entry(z: zipfile.ZipFile, name: str, max_bytes: int) -> bytes:
    """Read one entry, refusing it as soon as it passes max_bytes."""
    out = bytearray()
    try:
        with z.open(name) as f:
            while chunk := f.read(CHUNK):
                out += chunk
                if len(out) > max_bytes:
                    raise FileRefused("zip_bomb")
    except (KeyError, FileRefused):
        raise
    except Exception:
        raise FileRefused("damaged_file") from None
    return bytes(out)


def _sniff_zip(data: bytes, caps: SafetyCaps) -> str:
    z = open_zip(data, caps)
    names = set(z.namelist())
    if "word/document.xml" in names:
        kind = "docx"
    elif "ppt/presentation.xml" in names:
        kind = "pptx"
    elif "xl/workbook.xml" in names:
        kind = "xlsx"
    elif "mimetype" in names:
        mimetype = read_entry(z, "mimetype", 256).decode("ascii", "replace").strip()
        kind = ODF_KINDS.get(mimetype)
        if kind is None:
            raise FileRefused("unsupported_type")
        if "META-INF/manifest.xml" in names:
            manifest = read_entry(z, "META-INF/manifest.xml", int(caps.max_entry_mb * MB))
            if b"manifest:encryption-data" in manifest:
                raise FileRefused("encrypted")
    else:
        raise FileRefused("unsupported_type")
    if caps.reject_macros and any(n.endswith("/vbaProject.bin") or n.startswith("Basic/") for n in names):
        raise FileRefused("macros")
    return kind


# ---- OLE (old Office) -----------------------------------------------------


def _sniff_ole(data: bytes, caps: SafetyCaps) -> str:
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except Exception:
        raise FileRefused("damaged_file") from None
    try:
        if ole.exists("EncryptedPackage"):
            raise FileRefused("encrypted")  # a password-protected DOCX, XLSX or PPTX
        if ole.exists("WordDocument"):
            kind = "doc"
            fib = _read_stream(ole, "WordDocument", 12)
            if len(fib) < 12 or struct.unpack_from("<H", fib, 0x0A)[0] & 0x0100:
                raise FileRefused("encrypted")
        elif ole.exists("Workbook") or ole.exists("Book"):
            kind = "xls"
            stream = "Workbook" if ole.exists("Workbook") else "Book"
            if _xls_has_filepass(_read_stream(ole, stream, CHUNK)):
                raise FileRefused("encrypted")
        elif ole.exists("PowerPoint Document"):
            kind = "ppt"
            if ole.exists("EncryptedSummary"):
                raise FileRefused("encrypted")
        else:
            raise FileRefused("unsupported_type")
        if caps.reject_macros and (ole.exists("Macros") or ole.exists("_VBA_PROJECT_CUR")):
            raise FileRefused("macros")
        return kind
    finally:
        ole.close()


def _read_stream(ole, name: str, n: int) -> bytes:
    try:
        with ole.openstream(name) as f:
            return f.read(n)
    except Exception:
        raise FileRefused("damaged_file") from None


def _xls_has_filepass(stream: bytes) -> bool:
    """A FILEPASS record (0x002F) after the first BOF, before its EOF."""
    pos, seen_bof = 0, False
    while pos + 4 <= len(stream):
        rtype, length = struct.unpack_from("<HH", stream, pos)
        if rtype in (0x0809, 0x0409, 0x0209, 0x0009):
            seen_bof = True
        elif seen_bof and rtype == 0x002F:
            return True
        elif seen_bof and rtype == 0x000A:
            return False
        pos += 4 + length
    return False


# ---- text -------------------------------------------------------------------


def _sniff_text(data: bytes, filename_hint: str | None) -> str:
    sample = data[:SAMPLE_BYTES]
    decoded = decode_text(sample)
    if decoded is None or not decoded.text:
        raise FileRefused("unsupported_type")
    text = decoded.text
    control = sum(1 for c in text if unicodedata.category(c) == "Cc" and c not in "\t\n\r\f\v")
    if control / len(text) >= 0.01:
        raise FileRefused("unsupported_type")
    head = text.lstrip("﻿").lstrip()[:20].lower()
    if head.startswith(("<!doctype html", "<html")):
        return "html"
    if _looks_like_csv(text, cut=len(data) > SAMPLE_BYTES):
        return "csv"
    hint = (filename_hint or "").lower()
    if hint.endswith((".md", ".markdown")):
        return "md"
    return "txt"


def _looks_like_csv(text: str, cut: bool) -> bool:
    """The column rule: a delimiter, 5+ non-empty lines, and 90% of them with
    the same column count of 2 or more. Prose with commas fails it."""
    lines = [line for line in text.splitlines() if line.strip()]
    if cut and lines:
        lines = lines[:-1]  # the sample may end mid-line
    if len(lines) < 5:
        return False
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines), delimiters=",;\t|")
    except csv.Error:
        return False
    counts = Counter(len(row) for row in csv.reader(lines, dialect))
    columns, n = counts.most_common(1)[0]
    return columns >= 2 and n / len(lines) >= 0.9
