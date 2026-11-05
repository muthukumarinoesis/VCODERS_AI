"""Against the real system tools (Tesseract, pdftoppm, LibreOffice). Each
test skips when its tool isn't installed; the Docker image has them all:

    docker build --build-arg REQUIREMENTS=requirements-dev.txt -t vcoders/quiz-service:test .
    docker run --rm vcoders/quiz-service:test python -m pytest tests/test_system.py
"""

import io
import multiprocessing
import os
import shutil
import subprocess
import tempfile

import pebble
import pytest
from fixtures import ENGLISH, TAMIL, docx_bytes, page_picture, pptx_bytes, text_pdf, xlsx_bytes
from PIL import Image, ImageDraw, ImageFont

from pipeline_router import Prober, SafetyCaps, TesseractScriptDetector, limit_child_memory, sniff
from pipeline_router.probe import render_page_png
from quiz_runner import read

TAMIL_FONT = "/usr/share/fonts/truetype/lohit-tamil/Lohit-Tamil.ttf"
SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")


def needs(tool):
    return pytest.mark.skipif(shutil.which(tool) is None, reason=f"{tool} isn't installed")


def png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


TAMIL_LINES = [
    "செல் என்பது உயிரின் அடிப்படை அலகு ஆகும்.",
    "தாவரங்கள் ஒளிச்சேர்க்கை மூலம் உணவு தயாரிக்கின்றன.",
    "பச்சையம் சூரிய ஒளியை உறிஞ்சுகிறது.",
    "நீர் மற்றும் கார்பன் டை ஆக்சைடு தேவைப்படுகிறது.",
    "விலங்குகள் தாவரங்களை உணவாக உட்கொள்கின்றன.",
    "இதயம் இரத்தத்தை உடல் முழுவதும் செலுத்துகிறது.",
]


def tamil_page() -> Image.Image:
    """A 300 dpi page of textbook-sized (about 10 pt) Tamil text."""
    img = Image.new("RGB", (2480, 3508), "white")
    draw, font = ImageDraw.Draw(img), ImageFont.truetype(TAMIL_FONT, 40)
    for i in range(40):
        line = TAMIL_LINES[i % 6] + " " + TAMIL_LINES[(i + 3) % 6][:20]
        draw.text((150, 150 + i * 80), line, fill="black", font=font)
    return img


@needs("tesseract")
def test_tesseract_finds_the_script():
    detector = TesseractScriptDetector()
    assert detector.detect(png(page_picture(2480)), timeout_s=20)[0] == "Latn"
    if os.path.exists(TAMIL_FONT):
        assert detector.detect(png(tamil_page()), timeout_s=20)[0] == "Taml"


def _detect_in_child(image: bytes):
    return TesseractScriptDetector().detect(image, timeout_s=20)


@needs("tesseract")
def test_tesseract_works_under_the_child_memory_limit():
    """Spec 9.1: test OCR on a real scan under the limit (probe_memory_mb)."""
    pool = pebble.ProcessPool(max_workers=1, initializer=limit_child_memory,
                              initargs=(SafetyCaps().probe_memory_mb,),
                              context=multiprocessing.get_context("spawn"))  # fmt: skip
    try:
        result = pool.schedule(_detect_in_child, args=(png(page_picture(2480)),), timeout=60).result()
    finally:
        pool.stop()
        pool.join()
    assert result is not None and result[0] == "Latn"


@needs("tesseract")
def test_scanned_pdf_language_with_real_tesseract():
    from fixtures import scanned_pdf

    prober = Prober(SafetyCaps(), image_script_detector=TesseractScriptDetector(), scan_time_budget_s=20)
    facts = prober.probe(scanned_pdf(3, picture=page_picture(2480)), "pdf")
    assert (facts.language, facts.language_source) == ("en", "ocr script detection")


@needs("pdftoppm")
def test_pdftoppm_renders_a_page():
    image = render_page_png(text_pdf(2), 2, timeout_s=20)
    assert image.startswith(b"\x89PNG") and Image.open(io.BytesIO(image)).width > 1000


def _convert(data: bytes, src_ext: str, target: str) -> bytes:
    """Makes a real old-format Office file with LibreOffice."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, f"in.{src_ext}")
        with open(src, "wb") as f:
            f.write(data)
        subprocess.run([SOFFICE, f"-env:UserInstallation=file://{tmp}/profile", "--headless",
                        "--convert-to", target, "--outdir", tmp, src],
                       capture_output=True, timeout=180, check=True)  # fmt: skip
        with open(os.path.join(tmp, f"in.{target}"), "rb") as f:
            return f.read()


@pytest.mark.skipif(SOFFICE is None, reason="LibreOffice isn't installed")
@pytest.mark.parametrize("make,src,old", [
    (lambda: docx_bytes([TAMIL * 4, ENGLISH]), "docx", "doc"),
    (lambda: xlsx_bytes([[w] for w in (ENGLISH * 2).split()]), "xlsx", "xls"),
    (lambda: pptx_bytes([ENGLISH] * 3), "pptx", "ppt"),
])  # fmt: skip
def test_real_old_office_files(make, src, old):
    data = _convert(make(), src, old)
    caps = SafetyCaps()
    assert sniff(data, caps) == old  # real OLE files, not only the generated test ones
    facts = Prober(caps).probe(data, old)
    assert facts.language in ("ta", "en")
    readers = {"doc": {"impl": "libreoffice", "then": "python_docx"},
               "xls": {"impl": "libreoffice", "then": "openpyxl"},
               "ppt": {"impl": "libreoffice", "then": "python_pptx"}}  # fmt: skip
    text = " ".join(p.text for p in read(old, data, readers))
    assert "cell" in text.lower() or "தமிழ்" in text
