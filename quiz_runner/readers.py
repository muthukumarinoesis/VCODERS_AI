"""Full readers for the worker, chosen by each pipeline's `readers:` config.

The file was already checked (type, zip bomb, encryption, pages) when it
was inspected, so readers only read.
"""

import io
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from PIL import Image

from pipeline_router.caps import MB, SafetyCaps
from pipeline_router.filetype import open_zip, read_entry
from pipeline_router.probe import _pillow_guard, markup_to_text, rtf_to_text
from pipeline_router.formulas import has_formulas, is_math_font
from pipeline_router.quality import QualityRules, garbled_share
from pipeline_router.textdecode import decode_text

from .errors import RunnerError
from .omml import paragraph_text

MIN_PAGE_CHARS = 40  # fewer real characters than this: read the page from its picture
RENDER_DPI = 150
CONVERT_TIMEOUT_S = 120


@dataclass
class Page:
    number: int
    text: str = ""
    needs_ocr: bool = False  # read it from the picture (no usable text layer, or formulas)
    image: bytes | None = None  # PNG, for image files or once a PDF page is rendered


def _visible(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


def read_pdf(data: bytes, caps: SafetyCaps, formulas: str = "picture", **_) -> list[Page]:
    """A page with too little text, or garbled text (a broken font encoding),
    is read from its picture instead. So is a formula-heavy page, unless the
    reader config says `formulas: text`; it keeps its text layer as a
    fallback in case it isn't among the pages read from pictures."""
    import pymupdf

    garbled = QualityRules().garbled_page
    pages = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            doc.authenticate("")  # owner-password-only PDFs were accepted at inspection
        for i, page in enumerate(doc):
            text = page.get_text()
            if _visible(text) < MIN_PAGE_CHARS or garbled_share(text) >= garbled:
                pages.append(Page(i + 1, "", needs_ocr=True))
                continue
            math = formulas == "picture" and has_formulas(text, _math_font_share(page))
            pages.append(Page(i + 1, text, needs_ocr=math))
    return pages


def _math_font_share(page) -> float:
    total = math = 0
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", ()):
            for span in line["spans"]:
                n = _visible(span["text"])
                total += n
                math += n if is_math_font(span["font"]) else 0
    return math / total if total else 0.0


def render_pdf_pages(data: bytes, numbers) -> dict[int, bytes]:
    """PNG of each 1-based page number."""
    import pymupdf

    out = {}
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            doc.authenticate("")
        for n in numbers:
            out[n] = doc[n - 1].get_pixmap(dpi=RENDER_DPI).tobytes("png")
    return out


def read_docx(data: bytes, caps: SafetyCaps, **_) -> list[Page]:
    import docx

    d = docx.Document(io.BytesIO(data))
    parts = [paragraph_text(p._p) for p in d.paragraphs]  # equations become $LaTeX$
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return [Page(1, "\n".join(parts))]


def read_pptx(data: bytes, caps: SafetyCaps, **_) -> list[Page]:
    import pptx

    pages = []
    for i, slide in enumerate(pptx.Presentation(io.BytesIO(data)).slides):
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append(" | ".join(cell.text for cell in row.cells))
        if slide.has_notes_slide:
            parts.append(slide.notes_slide.notes_text_frame.text)
        pages.append(Page(i + 1, "\n".join(parts)))
    return pages


def read_xlsx(data: bytes, caps: SafetyCaps, **_) -> list[Page]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        pages = []
        for i, ws in enumerate(wb.worksheets):
            rows = (
                " | ".join(str(v) for v in row if v is not None) for row in ws.iter_rows(values_only=True)
            )
            pages.append(Page(i + 1, f"{ws.title}\n" + "\n".join(r for r in rows if r)))
        return pages
    finally:
        wb.close()


def read_odf(data: bytes, caps: SafetyCaps, **_) -> list[Page]:
    """Through the same capped ZIP helpers as inspection."""
    z = open_zip(data, caps)
    xml = read_entry(z, "content.xml", int(caps.max_entry_mb * MB)).decode("utf-8", "replace")
    for end in ("</text:p>", "</text:h>", "</table:table-row>", "</draw:page>"):
        xml = xml.replace(end, end + "\n")
    return [Page(1, markup_to_text(xml))]


def read_rtf(data: bytes, caps: SafetyCaps, **_) -> list[Page]:
    return [Page(1, rtf_to_text(data))]


def _decoded(data: bytes) -> str:
    decoded = decode_text(data)
    if decoded is None:
        raise RunnerError(
            "no_text", "We couldn't read the text in this file. Save it as UTF-8 and try again."
        )
    return decoded.text


def read_plain(data: bytes, caps: SafetyCaps, **_) -> list[Page]:
    return [Page(1, _decoded(data))]


def read_html(data: bytes, caps: SafetyCaps, **_) -> list[Page]:
    return [Page(1, markup_to_text(_decoded(data)))]


def read_image(data: bytes, caps: SafetyCaps, **_) -> list[Page]:
    """Each frame (TIFF can have many) as a PNG page to read from the picture."""
    pages = []
    try:
        with _pillow_guard(int(caps.max_image_megapixels * 1_000_000)):
            with Image.open(io.BytesIO(data)) as img:
                for i in range(min(getattr(img, "n_frames", 1), caps.max_pages)):
                    img.seek(i)
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, "PNG")
                    pages.append(Page(i + 1, "", needs_ocr=True, image=buf.getvalue()))
    except Exception:
        raise RunnerError(
            "damaged_file", "This image can't be opened. Export it again and upload it."
        ) from None
    return pages


READERS = {
    "pymupdf": read_pdf,
    "python_docx": read_docx,
    "python_pptx": read_pptx,
    "openpyxl": read_xlsx,
    "odfpy": read_odf,  # the builder's name; served by the capped built-in ODF reader
    "odf": read_odf,
    "rtf": read_rtf,
    "plain": read_plain,
    "csv": read_plain,
    "html": read_html,
    "cached_ocr": read_image,  # pictures are read by the OCR step (cached by image hash)
}
CONVERT_TO = {"python_docx": "docx", "python_pptx": "pptx", "openpyxl": "xlsx"}


def libreoffice_convert(data: bytes, kind: str, target: str, timeout_s: float = CONVERT_TIMEOUT_S) -> bytes:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if exe is None:
        raise RunnerError(
            "conversion_unavailable",
            f"{kind.upper()} files can't be read right now. "
            f"Save it as PDF or {target.upper()} and upload it again.",
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, f"in.{kind}")
        with open(src, "wb") as f:
            f.write(data)
        cmd = [exe, f"-env:UserInstallation=file://{tmp}/profile", "--headless",
               "--convert-to", target, "--outdir", tmp, src]  # fmt: skip
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=True)
            with open(os.path.join(tmp, f"in.{target}"), "rb") as f:
                return f.read()
        except (subprocess.SubprocessError, OSError):
            raise RunnerError(
                "conversion_failed",
                f"This {kind.upper()} file couldn't be converted. "
                f"Save it as PDF or {target.upper()} and try again.",
            ) from None


def read(kind: str, data: bytes, readers: dict, caps: SafetyCaps | None = None) -> list[Page]:
    caps = caps or SafetyCaps()
    spec = readers.get(kind)
    if not spec:
        raise RunnerError("no_reader", f"This pipeline can't read {kind} files.")
    impl = spec.get("impl")
    options = {k: v for k, v in spec.items() if k not in ("impl", "then")}
    if impl == "libreoffice":
        then = spec.get("then")
        if then not in CONVERT_TO:
            raise RunnerError(
                "bad_config", f"Reader for {kind}: 'then' must be one of {', '.join(CONVERT_TO)}."
            )
        return READERS[then](libreoffice_convert(data, kind, CONVERT_TO[then]), caps, **options)
    if impl not in READERS:
        raise RunnerError("bad_config", f"Unknown reader {impl!r} for {kind}.")
    return READERS[impl](data, caps, **options)
