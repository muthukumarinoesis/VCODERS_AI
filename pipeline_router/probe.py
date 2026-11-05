"""Cheap facts about a file, plus the PDF safety checks (spec 5.4).

Only sampled parts are read, never the whole document.
"""

import contextlib
import dataclasses
import glob
import html
import io
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
import warnings
from collections import Counter
from dataclasses import dataclass

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.errors import DependencyError

from .caps import MB, SafetyCaps
from .filetype import FileRefused, open_zip, read_entry
from .formulas import has_formulas, is_math_font
from .quality import QualityRules, file_quality, garbled_share, image_quality, rejection_message
from .scripts import APPROXIMATE, LANGUAGE, SCRIPT_OF_LANGUAGE, dominant_language
from .textdecode import decode_text

LEGACY_TAMIL_FONT = re.compile(
    r"bamini|baamini|vanavil|tscii|tsc_|elango|kalaham|shree-?tam|\btam-|\btab-", re.I
)
LEGACY_FONT_SHARE = 0.30
SAMPLE_CHARS = 2000
CHARS_PER_PAGE = 3000
WORDS_PER_PAGE = 400
ROWS_PER_PAGE = 50
MAX_CELLS = 200
MAX_DETECTOR_PX = 2000
OLE_MAIN_STREAMS = ("WordDocument", "PowerPoint Document", "Workbook", "Book")
PDF_DAMAGED = "This PDF is damaged and can't be read. Try saving or exporting it again."
PDF_ENCRYPTED = "This PDF is password-protected. Remove the password and upload it again."


@dataclass(frozen=True)
class FileFacts:
    kind: str | None  # None for topic requests
    pages: int  # pages, slides, or sheets x rows/50; estimated for text
    has_text_layer: bool
    text_page_share: float
    language: str | None
    script: str | None
    language_confidence: float
    language_approximate: bool
    language_source: str  # "text" | "request" | "legacy font" | "ocr script detection"
    # | "suspected legacy encoding" | "unreadable scan" | "timed out" | "none"
    legacy_font_share: float = 0.0  # share of sampled text in legacy Tamil fonts
    sample_text: str = ""  # about 2,000 chars from spread pages, legacy-font text excluded
    probe_ms: int = 0
    quality_score: int | None = None  # 0-100 from the sampled page pictures; None when there are none
    quality_issues: tuple = ()  # e.g. ("blurry", "dark"): what to tell the user when the score is low
    formulas: bool = False  # formula-heavy: the worker reads such PDF pages from their pictures

    @property
    def scanned(self) -> bool:
        return not self.has_text_layer

    def to_dict(self) -> dict:  # JSON types only: stored with the upload
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FileFacts":  # re-routes corrections without re-inspecting
        names = {f.name for f in dataclasses.fields(cls)}
        d = {k: v for k, v in d.items() if k in names}
        if "quality_issues" in d:
            d["quality_issues"] = tuple(d["quality_issues"])
        return cls(**d)


def sample_indices(n: int, k: int) -> list[int]:
    """Up to k page numbers spread through the document, skipping the cover
    (page 0) when there are more than 2 pages."""
    pool = list(range(1 if n > 2 else 0, n))
    if len(pool) <= k:
        return pool
    if k <= 1:
        return pool[:k]
    return sorted({pool[round(i * (len(pool) - 1) / (k - 1))] for i in range(k)})


def sample_text(text: str, limit: int = SAMPLE_CHARS) -> str:
    """Whitespace collapsed; at most `limit` chars as 4 slices from the
    start, middle and end."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    sep = " … "
    size = (limit - 3 * len(sep)) // 4
    starts = [0, len(text) // 3, 2 * len(text) // 3, len(text) - size]
    return sep.join(text[s : s + size] for s in starts)


def _language(text: str) -> dict:
    g = dominant_language(text)
    return dict(
        language=g.language,
        script=g.script,
        language_confidence=round(g.confidence, 3),
        language_approximate=g.approximate,
        language_source="text" if g.language else "none",
    )


NO_LANGUAGE = dict(language=None, script=None, language_confidence=0.0, language_approximate=False)


def _visible(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


class TesseractScriptDetector:
    """Script of a page image via Tesseract OSD. Never raises."""

    SCRIPTS = {
        "Latin": "Latn", "Tamil": "Taml", "Devanagari": "Deva", "Telugu": "Telu",
        "Kannada": "Knda", "Malayalam": "Mlym", "Bengali": "Beng", "Oriya": "Orya",
        "Sinhala": "Sinh", "Arabic": "Arab", "Gujarati": "Gujr", "Gurmukhi": "Guru",
        "Cyrillic": "Cyrl", "Greek": "Grek", "Hebrew": "Hebr", "Thai": "Thai",
        "Hangul": "Hang", "Han": "Hani", "Japanese": "Kana",
    }  # fmt: skip

    def __init__(self, min_confidence: float = 0.4):
        self.min_confidence = min_confidence

    def detect(self, image: bytes, timeout_s: float) -> tuple[str, float] | None:
        try:
            import pytesseract

            with Image.open(io.BytesIO(image)) as img:
                img.thumbnail((MAX_DETECTOR_PX, MAX_DETECTOR_PX))
                osd = pytesseract.image_to_osd(
                    img, output_type=pytesseract.Output.DICT, timeout=max(timeout_s, 0.1)
                )
            code = self.SCRIPTS.get(osd.get("script"))
            confidence = min(1.0, float(osd.get("script_conf", 0)) / 5)
        except Exception:
            return None
        if code is None or confidence < self.min_confidence:
            return None
        return code, confidence


@contextlib.contextmanager
def _pillow_guard(max_pixels: int):
    """Same cap as the files; a decompression bomb becomes an error."""
    old = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            yield
    finally:
        Image.MAX_IMAGE_PIXELS = old


def render_page_png(data: bytes, page_number: int, timeout_s: float) -> bytes | None:
    """One PDF page (1-based) as PNG via pdftoppm, or None if it's missing or fails."""
    exe = shutil.which("pdftoppm")
    if exe is None or timeout_s <= 0:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        with open(src, "wb") as f:
            f.write(data)
        n = str(page_number)
        try:
            subprocess.run(
                [exe, "-r", "150", "-f", n, "-l", n, "-png", src, os.path.join(tmp, "out")],
                capture_output=True, timeout=timeout_s, check=True,
            )  # fmt: skip
        except (subprocess.SubprocessError, OSError):
            return None
        out = glob.glob(os.path.join(tmp, "out*.png"))
        if not out:
            return None
        with open(out[0], "rb") as f:
            return f.read()


def pdf_crypto_self_test() -> None:
    """Startup check: AES-encrypted PDFs must open, or every one of them
    would be refused as damaged."""
    try:
        w = PdfWriter()
        w.add_blank_page(72, 72)
        w.encrypt(user_password="", owner_password="owner", algorithm="AES-256")
        buf = io.BytesIO()
        w.write(buf)
        r = PdfReader(io.BytesIO(buf.getvalue()))
        r.decrypt("")
        len(r.pages)
    except (DependencyError, ImportError) as e:
        raise RuntimeError(
            "pypdf can't open AES-encrypted PDFs: install pypdf[crypto] " f"(the cryptography package). {e}"
        ) from e


class Prober:
    def __init__(
        self,
        caps: SafetyCaps,
        image_script_detector=None,
        sample_pages: int = 4,
        min_chars_per_page: int = 40,
        max_scan_images: int = 2,
        scan_time_budget_s: float = 6.0,
        quality: QualityRules | None = QualityRules(),
    ):
        self.caps = caps
        self.detector = image_script_detector
        self.sample_pages = sample_pages
        self.min_chars_per_page = min_chars_per_page
        self.max_scan_images = max_scan_images
        self.scan_time_budget_s = scan_time_budget_s
        self.quality = quality  # None turns the page quality check off

    # ---- entry points ---------------------------------------------------

    def probe(self, data: bytes, kind: str) -> FileFacts:
        t0 = time.monotonic()
        reader = getattr(self, f"_probe_{kind}", None)
        if reader is None:
            raise FileRefused("unsupported_type")
        try:
            fields = reader(data, t0)
        except (FileRefused, DependencyError):
            raise
        except Exception:
            raise FileRefused("damaged_file") from None
        return self._facts(kind, t0, **fields)

    def text_facts(self, text: str) -> FileFacts:
        t0 = time.monotonic()
        if len(text.encode("utf-8")) > self.caps.max_text_kb * 1024:
            raise FileRefused(
                "too_large", f"Pasted text is limited to {self.caps.max_text_kb} KB. Paste less text."
            )
        return self._facts("txt", t0, **self._text_fields(text))

    def topic_facts(self, topic: str, language: str | None = None) -> FileFacts:
        t0 = time.monotonic()
        topic = " ".join(topic.split())
        if language:
            language = language.strip().lower()
            script = SCRIPT_OF_LANGUAGE.get(language)
            lang = dict(language=language, script=script, language_confidence=1.0,
                        language_approximate=False, language_source="request")  # fmt: skip
        elif len(topic) >= 50:
            lang = _language(topic)
        else:
            lang = dict(NO_LANGUAGE, language_source="none")
        return self._facts(None, t0, pages=0, text=topic, **lang)

    # ---- shared ---------------------------------------------------------

    def _facts(self, kind, t0, pages, text="", has_text_layer=True, text_page_share=1.0,
               legacy_font_share=0.0, quality_score=None, quality_issues=(),
               **lang) -> FileFacts:  # fmt: skip
        return FileFacts(
            quality_score=quality_score,
            quality_issues=tuple(quality_issues),
            kind=kind,
            pages=pages,
            has_text_layer=has_text_layer,
            text_page_share=round(text_page_share, 3),
            legacy_font_share=round(legacy_font_share, 3),
            sample_text=sample_text(text),
            probe_ms=int((time.monotonic() - t0) * 1000),
            **lang,
        )

    def _text_fields(self, text: str, pages: int | None = None, legacy_font_share=0.0) -> dict:
        if pages is None:
            pages = max(1, math.ceil(len(text) / CHARS_PER_PAGE))
        return dict(pages=pages, text=text, legacy_font_share=legacy_font_share,
                    formulas=has_formulas(text), **_language(text))  # fmt: skip

    def _font_fields(self, chunks: list[tuple[str, str]], pages: int) -> dict:
        """chunks are (font name, text). Legacy-font text is gibberish: at 30%
        or more the file is Tamil, below it that text is left out."""
        total = sum(_visible(t) for _, t in chunks)
        legacy = sum(_visible(t) for f, t in chunks if LEGACY_TAMIL_FONT.search(f or ""))
        share = legacy / total if total else 0.0
        clean = " ".join(t for f, t in chunks if not LEGACY_TAMIL_FONT.search(f or ""))
        fields = self._text_fields(clean, pages, share)
        if share >= LEGACY_FONT_SHARE:
            fields.update(language="ta", script="Taml", language_approximate=False,
                          language_confidence=round(min(0.95, 0.5 + share / 2), 3),
                          language_source="legacy font")  # fmt: skip
        return fields

    # ---- PDF ------------------------------------------------------------

    def _open_pdf(self, data: bytes):
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                try:
                    decrypted = reader.decrypt("")
                except DependencyError:
                    raise
                except Exception:
                    decrypted = 0
                if not decrypted:
                    raise FileRefused("encrypted", PDF_ENCRYPTED)
            n = len(reader.pages)
        except (FileRefused, DependencyError):
            raise  # a missing crypto library is a server fault, never damaged_file
        except Exception:
            raise FileRefused("damaged_file", PDF_DAMAGED) from None
        if n == 0:
            raise FileRefused("damaged_file", PDF_DAMAGED)
        if n > self.caps.max_pages:
            raise FileRefused(
                "too_many_pages",
                f"This PDF has {n} pages; the limit is {self.caps.max_pages}. Split it into smaller parts.",
            )
        return reader, n

    def _has_javascript(self, data: bytes, reader, indices) -> bool:
        try:
            root = reader.trailer["/Root"].get_object()
            names = root.get("/Names")
            if names is not None and "/JavaScript" in names.get_object():
                return True
            action = root.get("/OpenAction")
            action = action.get_object() if action is not None else None
            if hasattr(action, "get") and (action.get("/S") == "/JavaScript" or "/JS" in action):
                return True
            if "/AA" in root or any("/AA" in reader.pages[i] for i in indices):
                return True
        except Exception:
            raise FileRefused("damaged_file", PDF_DAMAGED) from None
        return b"/JavaScript" in data

    def _probe_pdf(self, data: bytes, t0: float) -> dict:
        reader, n = self._open_pdf(data)
        indices = sample_indices(n, self.sample_pages)
        if self.caps.reject_pdf_js and self._has_javascript(data, reader, indices):
            raise FileRefused(
                "pdf_javascript",
                "PDFs with JavaScript aren't accepted. Print or export it again as a plain PDF.",
            )
        chunks, with_text, garbled = [], 0, 0
        for i in indices:
            page_chunks = []

            def visit(text, cm, tm, font_dict, font_size, out=page_chunks):
                if text:
                    font = str(font_dict.get("/BaseFont", "")) if font_dict else ""
                    out.append((re.sub(r"^/?([A-Z]{6}\+)?", "", font), text))

            try:
                reader.pages[i].extract_text(visitor_text=visit)
            except Exception:
                pass  # an unreadable page counts as a page without text
            if sum(_visible(t) for _, t in page_chunks) >= self.min_chars_per_page:
                if (
                    self.quality
                    and garbled_share("".join(t for _, t in page_chunks)) >= self.quality.garbled_page
                ):
                    garbled += 1  # broken font encoding: the worker reads this page from its picture
                    continue
                with_text += 1
            chunks += page_chunks
        share = with_text / len(indices)
        if share >= 0.5:
            fields = self._font_fields(chunks, n)
            total = sum(_visible(t) for _, t in chunks) or 1
            math_fonts = sum(_visible(t) for f, t in chunks if is_math_font(f)) / total
            fields["formulas"] = has_formulas(fields["text"], math_fonts)
            return dict(text_page_share=share, **fields)
        images = list(self._pdf_images(data, reader, indices, t0))
        fields = self._scan(images, t0)
        if garbled:
            fields["quality_issues"] = tuple(sorted({*fields["quality_issues"], "garbled"}))
        return dict(pages=n, has_text_layer=False, text_page_share=share, **fields)

    def _pdf_images(self, data, reader, indices, t0):
        """Yields (image bytes or None when unreadable, page width in inches)
        from the sampled pages, checking the declared size before anything is
        decoded."""
        max_px = int(self.caps.max_image_megapixels * 1_000_000)
        found = 0
        for i in indices:
            page = reader.pages[i]
            try:
                xobjects = page["/Resources"].get_object()["/XObject"].get_object()
                images = {n: r.get_object() for n, r in xobjects.items()}
                names = [
                    n
                    for n, obj in images.items()
                    if obj.get("/Subtype") == "/Image"
                    and int(obj.get("/Width", 0)) * int(obj.get("/Height", 0)) <= max_px
                ]
            except Exception:
                names = []
            for name in names:
                if found >= self.max_scan_images:
                    return
                found += 1
                yield self._decode_pdf_image(data, page, name, i, t0), float(page.mediabox.width) / 72

    def _decode_pdf_image(self, data, page, name, index, t0) -> bytes | None:
        try:
            with _pillow_guard(int(self.caps.max_image_megapixels * 1_000_000)):
                img = page.images[name].image
                buf = io.BytesIO()
                img.save(buf, "PNG")
                return buf.getvalue()
        except Exception:
            # JBIG2 and CCITT scans: render the page instead.
            return render_page_png(data, index + 1, self._remaining(t0))

    def _remaining(self, t0: float) -> float:
        return self.scan_time_budget_s - (time.monotonic() - t0)

    def _scan(self, images: list, t0: float) -> dict:
        """Page quality, then script detection within the time budget.
        `images` are (bytes or None, page width in inches or None)."""
        fields = self._quality([(img, width) for img, width in images if img is not None], t0)
        return {**fields, **self._script([img for img, _ in images], t0)}

    def _quality(self, images, t0: float) -> dict:
        """Refuses the file when its sampled pages are too poor to read. Like
        script detection it stays within the scan budget; the worker checks
        every page again before reading it."""
        if not self.quality or not images:
            return dict(quality_score=None, quality_issues=())
        pages = []
        for img, width in images:
            if self._remaining(t0) <= 0:
                break
            try:
                pages.append(image_quality(img, width))
            except Exception:
                continue  # an image Pillow can't measure says nothing about quality
        score, issues = file_quality(pages, self.quality)
        if score is not None and score < self.quality.reject:
            raise FileRefused("poor_quality", rejection_message(issues))
        return dict(quality_score=score, quality_issues=issues)

    def _script(self, images, t0: float) -> dict:
        if self.detector is None:
            return dict(NO_LANGUAGE, language_source="none")
        best, readable, timed_out = None, 0, False
        for image in images:
            remaining = self._remaining(t0)
            if remaining <= 0:
                timed_out = True
                break
            if image is None:
                continue
            readable += 1
            result, finished = self._detect(image, remaining)
            if not finished:
                timed_out = True
                break
            if result and (best is None or result[1] > best[1]):
                best = result
        if best is not None:
            code, confidence = best
            return dict(language=LANGUAGE.get(code), script=code,
                        language_confidence=round(confidence, 3),
                        language_approximate=APPROXIMATE.get(code, False) and code != "Kana",
                        language_source="ocr script detection")  # fmt: skip
        source = "timed out" if timed_out else "unreadable scan" if not readable else "none"
        return dict(NO_LANGUAGE, language_source=source)

    def _detect(self, image: bytes, timeout_s: float):
        """Runs the detector in a thread so a stuck one can't overrun the budget."""
        out = []

        def run():
            try:
                out.append(self.detector.detect(image, timeout_s))
            except Exception:
                out.append(None)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout_s)
        return (out[0], True) if out else (None, False)

    # ---- images ---------------------------------------------------------

    def _probe_image(self, data: bytes, t0: float) -> dict:
        frames = []
        try:
            with _pillow_guard(int(self.caps.max_image_megapixels * 1_000_000)):
                with Image.open(io.BytesIO(data)) as img:
                    pages = getattr(img, "n_frames", 1)
                    if img.format == "TIFF":
                        for i in range(min(pages, 2)):
                            img.seek(i)
                            buf = io.BytesIO()
                            img.save(buf, "PNG")
                            frames.append((buf.getvalue(), None))
                    else:
                        img.load()
                        frames.append((data, None))
        except Exception:
            frames, pages = [(None, None)], 1
        fields = self._scan(frames, t0)
        return dict(pages=pages, has_text_layer=False, text_page_share=0.0, **fields)

    # ---- Office and ODF -------------------------------------------------

    def _probe_docx(self, data: bytes, t0: float) -> dict:
        import docx

        doc = docx.Document(io.BytesIO(data))
        try:
            normal = doc.styles["Normal"].font.name
        except KeyError:
            normal = None
        paragraphs = list(doc.paragraphs)
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    paragraphs += cell.paragraphs
        chunks, words = [], 0
        for p in paragraphs:
            style_font = p.style.font.name if p.style is not None else None
            for run in p.runs:
                chunks.append((run.font.name or style_font or normal or "", run.text))
            words += len(p.text.split())
        fields = self._font_fields(chunks, max(1, math.ceil(words / WORDS_PER_PAGE)))
        math_tag = "{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath"
        fields["formulas"] = fields["formulas"] or next(doc.element.body.iter(math_tag), None) is not None
        return fields

    def _probe_pptx(self, data: bytes, t0: float) -> dict:
        import pptx

        slides = list(pptx.Presentation(io.BytesIO(data)).slides)
        texts = []
        for i in sample_indices(len(slides), self.sample_pages):
            for shape in slides[i].shapes:
                if shape.has_text_frame:
                    texts.append(shape.text_frame.text)
        return self._text_fields("\n".join(texts), max(1, len(slides)))

    def _probe_xlsx(self, data: bytes, t0: float) -> dict:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            cells, rows = [], 0
            for n, ws in enumerate(wb.worksheets):
                seen = 0
                if n < 2:
                    for row in ws.iter_rows(values_only=True):
                        if len(cells) >= MAX_CELLS:
                            break
                        seen += 1
                        cells += [str(v) for v in row if v is not None][: MAX_CELLS - len(cells)]
                # read-only mode often reports None or 1 without a dimension record
                rows += max(ws.max_row or 0, seen)
        finally:
            wb.close()
        text = " ".join(cells)
        return self._text_fields(text, max(1, math.ceil(rows / ROWS_PER_PAGE)))

    def _probe_odf(self, data: bytes) -> dict:
        z = open_zip(data, self.caps)
        xml = read_entry(z, "content.xml", int(self.caps.max_entry_mb * MB))
        text = markup_to_text(xml.decode("utf-8", "replace"))
        return self._text_fields(text)

    def _probe_odt(self, data, t0):
        return self._probe_odf(data)

    _probe_odp = _probe_ods = _probe_odt

    def _probe_rtf(self, data: bytes, t0: float) -> dict:
        return self._text_fields(rtf_to_text(data))

    def _probe_doc(self, data: bytes, t0: float) -> dict:
        import olefile

        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            name = next(n for n in OLE_MAIN_STREAMS if ole.exists(n))
            with ole.openstream(name) as f:
                raw = f.read(2 * MB)
        text = ole_text(raw)
        return self._text_fields(text)

    _probe_xls = _probe_doc

    def _probe_ppt(self, data: bytes, t0: float) -> dict:
        """PowerPoint's stream is binary records that a string scan misreads,
        so the slide text is read from its text records instead."""
        import olefile

        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            with ole.openstream("PowerPoint Document") as f:
                raw = f.read(8 * MB)
        return self._text_fields(ppt_text(raw))

    # ---- text -----------------------------------------------------------

    def _probe_txt(self, data: bytes, t0: float, is_html: bool = False) -> dict:
        decoded = decode_text(data)
        if decoded is None:
            raise FileRefused("unsupported_type")
        text = decoded.text
        if is_html:
            text = markup_to_text(text)
        fields = self._text_fields(text)
        if decoded.legacy_encoding_suspected:
            fields.update(NO_LANGUAGE, language_source="suspected legacy encoding")
        return fields

    def _probe_html(self, data, t0):
        return self._probe_txt(data, t0, is_html=True)

    _probe_md = _probe_csv = _probe_txt


def markup_to_text(markup: str) -> str:
    """Text of HTML or ODF XML: script and style blocks and all tags dropped."""
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", markup)
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def _letter_runs(text: str, min_letters: int = 4) -> list[tuple[int, int]]:
    """(start, end) of runs of letters and marks (Indic vowel signs are
    marks), allowing single spaces, with at least `min_letters` letters."""
    runs, start, letters = [], None, 0
    for i, ch in enumerate(text + "\x00"):
        cat = unicodedata.category(ch)[0]
        if cat in "LM" or (ch == " " and start is not None):
            if start is None:
                start = i
            letters += cat == "L"
        else:
            if start is not None and letters >= min_letters:
                runs.append((start, i))
            start, letters = None, 0
    return runs


def _filler(run: str) -> bool:
    """Binary filler that happens to decode as letters (ÿÿÿÿ, ÌÿÿÿØ)."""
    chars = run.replace(" ", "")
    return Counter(chars).most_common(1)[0][1] / len(chars) > 0.5


def _ascii_letters(chunk: bytes) -> float:
    return sum(1 for c in chunk if 0x41 <= c <= 0x5A or 0x61 <= c <= 0x7A or c == 0x20) / max(len(chunk), 1)


PPT_TEXT_CHARS, PPT_TEXT_BYTES, PPT_CSTRING = 0x0FA0, 0x0FA8, 0x0FBA


def ppt_text(stream: bytes) -> str:
    """Text of a PowerPoint 97-2003 stream ([MS-PPT]): containers
    (recVer 0xF) are stepped into, TextCharsAtom and CString are UTF-16LE,
    TextBytesAtom is 8-bit."""
    out, pos = [], 0
    while pos + 8 <= len(stream):
        ver_instance, rtype, length = struct.unpack_from("<HHI", stream, pos)
        body = pos + 8
        if ver_instance & 0x0F == 0x0F:  # a container: its children follow
            pos = body
            continue
        if body + length > len(stream):
            break  # a damaged record: keep what was read
        chunk = stream[body : body + length]
        if rtype in (PPT_TEXT_CHARS, PPT_CSTRING):
            out.append(chunk.decode("utf-16-le", "replace"))
        elif rtype == PPT_TEXT_BYTES:
            out.append(chunk.decode("cp1252", "replace"))
        pos = body + length
    return "\n".join(t.replace("\r", "\n") for t in out)


def ole_text(raw: bytes) -> str:
    """Readable text in an old Office stream, for counting scripts only.

    UTF-16LE runs are read at both byte alignments, but a run whose bytes are
    mostly plain ASCII letters is 8-bit text misread as UTF-16 (two letters
    become one CJK character) and is dropped. 8-bit text is read as cp1252.
    Runs dominated by one character are binary filler."""
    out = []
    for offset in (0, 1):
        text = raw[offset : offset + (len(raw) - offset) // 2 * 2].decode("utf-16-le", "replace")
        for a, b in _letter_runs(text):
            if _ascii_letters(raw[offset + 2 * a : offset + 2 * b]) >= 0.7 or _filler(text[a:b]):
                continue
            out.append(text[a:b].strip())
    narrow = raw.decode("cp1252", "replace")
    out += [narrow[a:b].strip() for a, b in _letter_runs(narrow) if not _filler(narrow[a:b])]
    return " ".join(out)


_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer",
             "headerl", "headerr", "footerl", "footerr", "object", "listtable",
             "listoverridetable", "rsidtbl", "generator", "themedata", "colorschememapping",
             "datastore", "latentstyles", "xmlnstbl", "filetbl"}  # fmt: skip
_RTF_TOKEN = re.compile(
    rb"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}\r\n]+)|[\r\n]+", re.S
)


def rtf_to_text(data: bytes) -> str:
    """Plain text from RTF: groups like {\\*...} and the font table dropped,
    \\'hh and \\uN escapes decoded."""
    out, stack = [], []
    skip, uc, pending_skip, codepage = False, 1, 0, "cp1252"
    for m in _RTF_TOKEN.finditer(data):
        word, arg, hexbyte, sym, brace, plain = m.groups()
        if brace == b"{":
            stack.append((skip, uc))
            continue
        if brace == b"}":
            skip, uc = stack.pop() if stack else (False, 1)
            continue
        if pending_skip and (hexbyte or plain or sym):  # the \uN fallback characters
            if not plain:
                pending_skip -= 1
                continue
            n = min(len(plain), pending_skip)
            plain, pending_skip = plain[n:], pending_skip - n
            if not plain:
                continue
        if sym == b"*":
            skip = True
        elif word:
            w = word.decode()
            if w in _RTF_SKIP:
                skip = True
            elif w == "ansicpg" and arg:
                codepage = f"cp{int(arg)}"
            elif w == "uc" and arg:
                uc = int(arg)
            elif not skip and w == "u" and arg:
                out.append(chr(int(arg) % 65536))
                pending_skip = uc
            elif not skip and w in ("par", "line", "sect", "page"):
                out.append("\n")
            elif not skip and w in ("tab", "cell"):
                out.append(" ")
        elif skip:
            continue
        elif hexbyte:
            try:
                out.append(bytes([int(hexbyte, 16)]).decode(codepage, "replace"))
            except LookupError:
                out.append(bytes([int(hexbyte, 16)]).decode("cp1252", "replace"))
        elif sym:
            out.append(sym.decode("latin-1") if sym in b"\\{}" else " " if sym == b"~" else "")
        elif plain:
            out.append(plain.decode("latin-1"))
    return "".join(out)
