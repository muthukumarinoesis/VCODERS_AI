import io
import pickle
import random
import zipfile
from unittest.mock import patch

import pytest
from fixtures import (
    ENGLISH,
    docx_bytes,
    image_bytes,
    lie_about_size,
    odf_bytes,
    png_header,
    pptx_bytes,
    xlsx_bytes,
    zip_of,
)
from olegen import make_ole, word_stream, xls_stream

from pipeline_router import FileRefused, Prober, RoutingService, SafetyCaps, sniff


def refused(data, caps=None, hint=None):
    with pytest.raises(FileRefused) as e:
        sniff(data, caps or SafetyCaps(), hint)
    return e.value.code


def test_import():
    import pipeline_router  # noqa: F401  (mutable dataclass defaults would fail here)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "TIFF"])
def test_images(caps, fmt):
    assert sniff(image_bytes(fmt), caps) == "image"


def test_pdf_header(caps):
    assert sniff(b"%PDF-1.7\n...", caps) == "pdf"


def test_office_built_in_test(caps):
    assert sniff(docx_bytes(), caps) == "docx"
    assert sniff(pptx_bytes(["hello"]), caps) == "pptx"
    assert sniff(xlsx_bytes([["a", "b"]]), caps) == "xlsx"


@pytest.mark.parametrize("kind,expected", [("text", "odt"), ("presentation", "odp"), ("spreadsheet", "ods")])
def test_odf(caps, kind, expected):
    assert sniff(odf_bytes(kind), caps) == expected


def test_odf_with_password_is_encrypted():
    manifest = (
        b"<manifest:manifest><manifest:file-entry>"
        b'<manifest:encryption-data manifest:checksum="x"/>'
        b"</manifest:file-entry></manifest:manifest>"
    )
    assert refused(odf_bytes(manifest=manifest)) == "encrypted"


def test_rtf(caps):
    assert sniff(rb"{\rtf1\ansi hello}", caps) == "rtf"


def test_ole_kinds(caps):
    assert sniff(make_ole({"WordDocument": word_stream()}), caps) == "doc"
    assert sniff(make_ole({"Workbook": xls_stream()}), caps) == "xls"
    assert sniff(make_ole({"Book": xls_stream()}), caps) == "xls"
    assert sniff(make_ole({"PowerPoint Document": b"ppt"}), caps) == "ppt"


def test_ole_encryption_markers():
    assert refused(make_ole({"EncryptedPackage": b"x", "EncryptionInfo": b"y"})) == "encrypted"
    assert refused(make_ole({"WordDocument": word_stream(encrypted=True)})) == "encrypted"
    assert refused(make_ole({"Workbook": xls_stream(encrypted=True)})) == "encrypted"
    assert refused(make_ole({"PowerPoint Document": b"p", "EncryptedSummary": b"s"})) == "encrypted"


def test_ole_macros():
    assert refused(make_ole({"WordDocument": word_stream()}, storages=("Macros",))) == "macros"
    assert refused(make_ole({"Workbook": xls_stream()}, storages=("_VBA_PROJECT_CUR",))) == "macros"


def test_ole_other_is_unsupported():
    assert refused(make_ole({"Something": b"x"})) == "unsupported_type"


def test_macros_in_zip_and_odf():
    assert refused(docx_bytes(extra_entries={"word/vbaProject.bin": b"vba"})) == "macros"
    assert refused(odf_bytes(extra_entries={"Basic/Standard/Module1.xml": b"<x/>"})) == "macros"


def test_macros_allowed_when_not_rejected():
    caps = SafetyCaps(reject_macros=False)
    assert sniff(docx_bytes(extra_entries={"word/vbaProject.bin": b"vba"}), caps) == "docx"


# ---- zip bombs: small caps keep these fast -----------------------------------

SMALL = SafetyCaps(max_unpacked_mb=1, max_entry_mb=1)


def _inspect_without_docx(data, caps):
    service = RoutingService(None, Prober(caps), None, caps)
    with patch("docx.Document") as document:
        with pytest.raises(FileRefused) as e:
            service.inspect(data)
    document.assert_not_called()
    return e.value.code


def test_zip_over_unpack_cap():
    data = zip_of({"word/document.xml": b"\x00" * (5 * 2**20)})
    assert _inspect_without_docx(data, SMALL) == "zip_bomb"


def test_zip_ratio_over_100():
    data = zip_of({"word/document.xml": b"\x00" * 50_000})
    info = zipfile.ZipFile(io.BytesIO(data)).infolist()[0]
    # Only the whole-file ratio rule can catch this one.
    assert info.file_size / info.compress_size < 1000
    assert info.file_size / len(data) > 100
    assert refused(data) == "zip_bomb"


def test_zip_too_many_entries():
    data = zip_of({f"f{i}": b"" for i in range(10_001)}, zipfile.ZIP_STORED)
    assert refused(data) == "zip_bomb"


def test_zip_lying_about_entry_size():
    real = zip_of({"word/document.xml": b"\x00" * (5 * 2**20)})
    data = lie_about_size(real, 1024)
    assert zipfile.ZipFile(io.BytesIO(data)).getinfo("word/document.xml").file_size == 1024
    assert _inspect_without_docx(data, SMALL) == "zip_bomb"


def test_empty_random_damaged():
    assert refused(b"") == "empty_file"
    assert refused(random.Random(7).randbytes(4096)) == "unsupported_type"
    assert refused(b"PK\x03\x04" + b"\x00" * 200) == "damaged_file"


def test_too_large_by_length():
    assert refused(b"x" * (2 * 2**20), SafetyCaps(max_file_mb=1)) == "too_large"


def test_60_megapixel_png_header():
    assert refused(png_header(10_000, 6_000)) == "image_too_large"


def test_truncated_image_is_damaged():
    assert refused(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10) == "damaged_file"


def test_name_never_chooses_the_family(caps):
    assert sniff(xlsx_bytes([["a"]]), caps, "notes.pdf") == "xlsx"


def test_file_refused_pickles():
    e = pickle.loads(pickle.dumps(FileRefused("encrypted", "Remove the password.")))
    assert (e.code, e.message) == ("encrypted", "Remove the password.")


# ---- text kinds ----------------------------------------------------------------


def test_html_after_utf8_bom(caps):
    assert sniff(b"\xef\xbb\xbf  <!DOCTYPE html><html><body>hi</body></html>", caps) == "html"


def test_tamil_cut_mid_character_is_text(caps):
    data = ("a" + "தமிழ் மொழி " * 2000).encode("utf-8")
    assert data[:8192].decode("utf-8", "ignore") != data[:8192].decode("utf-8", "replace")
    assert sniff(data, caps) == "txt"


def test_csv_and_md(caps):
    rows = "\n".join(f"{i},name{i},{i * 3}" for i in range(10))
    assert sniff(rows.encode(), caps) == "csv"
    assert sniff(rows.encode(), caps, "data.csv") == "csv"
    assert sniff(ENGLISH.encode(), caps, "notes.md") == "md"


def test_prose_with_commas_is_never_csv(caps):
    prose = "\n".join(
        "Well, the cell, which is small, divides. Then, later, it grows again." if i % 2 else
        "Plants, animals and fungi are living things, as we know."
        for i in range(8)
    )  # fmt: skip
    assert sniff(prose.encode(), caps) == "txt"
    assert sniff(prose.encode(), caps, "notes.csv") == "txt"
