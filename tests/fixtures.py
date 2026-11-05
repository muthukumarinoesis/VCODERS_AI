"""Test files built in code (spec 10): PDFs, Office, ODF, images."""

import io
import struct
import zipfile
import zlib

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ENGLISH = (
    "The cell is the basic unit of life. Plants make food by photosynthesis "
    "using chlorophyll, water and carbon dioxide in the presence of sunlight. "
)
TAMIL = "தமிழ் ஒரு பழமையான மொழி. செல் உயிரின் அடிப்படை அலகு ஆகும். "
HINDI = "हिंदी भारत की एक प्रमुख भाषा है और यह देवनागरी लिपि में लिखी जाती है। "


def text_pdf(pages=6, body=ENGLISH, legacy_lines=0, legacy_body="Ftpij; xd;W", legacy_font="Bamini") -> bytes:
    """Helvetica text; `legacy_lines` lines per page in Courier, which is then
    renamed to the legacy Tamil font Bamini."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for p in range(pages):
        y = 800
        c.setFont("Courier", 12)
        for _ in range(legacy_lines):
            c.drawString(40, y, legacy_body)
            y -= 16
        c.setFont("Helvetica", 10)
        c.drawString(40, y, f"Page {p + 1}. {body[:90]}")
        c.drawString(40, y - 14, body[90:180])
        c.showPage()
    c.save()
    if not legacy_lines:
        return buf.getvalue()
    reader = PdfReader(io.BytesIO(buf.getvalue()))
    writer = PdfWriter(clone_from=reader)
    for page in writer.pages:
        for font in page["/Resources"]["/Font"].values():
            font = font.get_object()
            if font["/BaseFont"] == "/Courier":
                font[NameObject("/BaseFont")] = NameObject(f"/ABCDEF+{legacy_font}")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def page_picture(width=1240, lines=30, seed=1, mode="RGB"):
    """A clean picture of a page of text (A4 at 150 dpi by default)."""
    import random

    from PIL import Image, ImageDraw, ImageFont

    height = round(width * 11.69 / 8.27)
    img = Image.new(mode, (width, height), "white")
    draw, font = ImageDraw.Draw(img), ImageFont.load_default(size=max(8, width // 58))
    rnd, margin = random.Random(seed), width // 10
    for i in range(lines):
        words = " ".join(
            "".join(rnd.choice("abcdefghijklmnop") for _ in range(rnd.randint(2, 8))) for _ in range(9)
        )
        draw.text((margin, margin + i * (height - 2 * margin) // lines), words, fill="black", font=font)
    return img


def pdf_of(page_texts, font_path="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf") -> bytes:
    """One page per text, in a Unicode font, so symbols like ∫ and α are real text."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont("TestSans", font_path))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for text in page_texts:
        c.setFont("TestSans", 10)
        y = 800
        for line in [text[i : i + 95] for i in range(0, len(text), 95)][:60]:
            c.drawString(40, y, line)
            y -= 13
        c.showPage()
    c.save()
    return buf.getvalue()


def scanned_pdf(pages=3, vary=False, picture=None) -> bytes:
    """Each page is one full-page picture of text and no text layer; `vary`
    gives every page its own picture."""
    from reportlab.lib.utils import ImageReader

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for i in range(pages):
        if isinstance(picture, list):
            img = picture[i]
        else:
            img = picture if picture is not None else page_picture(seed=i if vary else 1)
        c.drawImage(ImageReader(img), 0, 0, *A4)
        c.showPage()
    c.save()
    return buf.getvalue()


def raw_pdf(objects: list[bytes]) -> bytes:
    """A PDF from numbered object bodies (1 = catalog), with a correct xref."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def image_xobject_pdf(width, height, filter_=b"/JBIG2Decode", data=b"not really an image") -> bytes:
    """One page showing one image XObject with the declared size."""
    content = b"q 100 0 0 100 0 0 cm /Im0 Do Q"
    return raw_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
        b"/BitsPerComponent 1 /Filter %s /Length %d >>\nstream\n" % (width, height, filter_, len(data))
        + data + b"\nendstream",
    ])  # fmt: skip


def docx_bytes(paragraphs=(ENGLISH,), extra_entries=None) -> bytes:
    import docx

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return add_entries(buf.getvalue(), extra_entries) if extra_entries else buf.getvalue()


def pptx_bytes(slide_texts) -> bytes:
    import pptx

    prs = pptx.Presentation()
    for text in slide_texts:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = "Lesson"
        slide.placeholders[1].text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def xlsx_bytes(rows, strip_dimension=False) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    if strip_dimension:
        data = rewrite_entry(
            data, "xl/worksheets/sheet1.xml",
            lambda xml: __import__("re").sub(rb"<dimension [^>]*/>", b"", xml),
        )  # fmt: skip
    return data


def odf_bytes(kind="text", text=ENGLISH, extra_entries=None, manifest=b"") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", f"application/vnd.oasis.opendocument.{kind}", zipfile.ZIP_STORED)
        z.writestr(
            "content.xml",
            '<?xml version="1.0"?><office:document-content><office:body><text:p>'
            f"{text}</text:p></office:body></office:document-content>",
            zipfile.ZIP_DEFLATED,
        )
        if manifest:
            z.writestr("META-INF/manifest.xml", manifest)
        for name, data in (extra_entries or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def add_entries(zip_data: bytes, entries: dict) -> bytes:
    buf = io.BytesIO(zip_data)
    with zipfile.ZipFile(buf, "a") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def rewrite_entry(zip_data: bytes, name: str, change) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(zip_data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            data = src.read(info)
            z.writestr(info, change(data) if info.filename == name else data)
    return buf.getvalue()


def zip_of(entries: dict, method=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def lie_about_size(zip_data: bytes, declared: int) -> bytes:
    """Patches the uncompressed size of every entry in both the local and the
    central directory headers."""
    data = bytearray(zip_data)
    pos = 0
    while (pos := data.find(b"PK\x03\x04", pos)) != -1:
        struct.pack_into("<I", data, pos + 22, declared)
        pos += 4
    pos = 0
    while (pos := data.find(b"PK\x01\x02", pos)) != -1:
        struct.pack_into("<I", data, pos + 24, declared)
        pos += 4
    return bytes(data)


def png_header(width, height) -> bytes:
    """A PNG whose header declares the size; the pixel data is a stub."""

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )


def image_bytes(fmt, img=None) -> bytes:
    """A tiny blank image by default (enough for type checks); pass `img` for a real page."""
    from PIL import Image

    buf = io.BytesIO()
    (img if img is not None else Image.new("RGB", (20, 10), "white")).save(buf, fmt)
    return buf.getvalue()


def page_photo(fmt="PNG") -> bytes:
    return image_bytes(fmt, page_picture())
