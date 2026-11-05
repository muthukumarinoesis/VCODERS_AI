import io
import time
from unittest.mock import patch

import pytest
from fixtures import image_xobject_pdf, scanned_pdf, text_pdf
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, TextStringObject

from pipeline_router import FileRefused, Prober, SafetyCaps, pdf_crypto_self_test, sample_indices


def probe(data, **kw):
    return Prober(SafetyCaps(), **kw).probe(data, "pdf")


def refused(data, **kw):
    with pytest.raises(FileRefused) as e:
        probe(data, **kw)
    return e.value.code


def encrypted(user, owner, algorithm="AES-256"):
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(text_pdf(2))))
    w.encrypt(user_password=user, owner_password=owner, algorithm=algorithm)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def written(w):
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class FakeDetector:
    def __init__(self, result=("Taml", 0.9), sleep=0.0):
        self.result, self.sleep, self.calls = result, sleep, 0

    def detect(self, image, timeout_s):
        self.calls += 1
        time.sleep(self.sleep)
        return self.result


# ---- safety -------------------------------------------------------------------


def test_user_password_is_encrypted():
    assert refused(encrypted("secret", "owner")) == "encrypted"


def test_owner_password_only_is_accepted_aes_and_rc4():
    assert probe(encrypted("", "owner", "AES-256")).language == "en"
    assert probe(encrypted("", "owner", "RC4-128")).language == "en"


@pytest.mark.parametrize("data", [
    b"%PDF-1.7\n this is not a pdf at all",
    text_pdf(3)[:400],
    b"%PDF-",
])  # fmt: skip
def test_damaged(data):
    assert refused(data) == "damaged_file"


def test_too_many_pages():
    w = PdfWriter()
    for _ in range(401):
        w.add_blank_page(72, 72)
    assert refused(written(w)) == "too_many_pages"


def test_add_js_is_refused():
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(text_pdf(2))))
    with pytest.warns(DeprecationWarning):  # still writes /Names /JavaScript
        w.add_js("app.alert('hi');")
    assert refused(written(w)) == "pdf_javascript"


def test_open_action_js_is_refused_without_raw_fallback():
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(text_pdf(2))))
    w._root_object[NameObject("/OpenAction")] = DictionaryObject(
        {NameObject("/S"): NameObject("/JavaScript"), NameObject("/JS"): TextStringObject("app.alert(1)")}
    )
    data = written(w)
    assert refused(data) == "pdf_javascript"
    # the structural check alone catches it, even if the raw bytes hide the word
    prober = Prober(SafetyCaps())
    reader, _ = prober._open_pdf(data)
    assert prober._has_javascript(b"", reader, [0])


def test_crypto_self_test_passes_and_fails_clearly():
    pdf_crypto_self_test()
    from pypdf.errors import DependencyError

    with patch("pypdf._encryption.aes_cbc_encrypt", side_effect=DependencyError("no crypto")):
        with pytest.raises(RuntimeError, match=r"pypdf\[crypto\]"):
            pdf_crypto_self_test()


def test_missing_crypto_is_never_damaged_file():
    from pypdf.errors import DependencyError

    data = encrypted("", "owner")
    with patch("pypdf._encryption.aes_cbc_decrypt", side_effect=DependencyError("no crypto")):
        with pytest.raises(DependencyError):
            probe(data)


# ---- content ------------------------------------------------------------------


def test_sample_indices_skip_the_cover():
    assert sample_indices(6, 4) == [1, 2, 4, 5]
    assert sample_indices(2, 4) == [0, 1]
    assert sample_indices(100, 4)[0] == 1 and len(sample_indices(100, 4)) == 4


def test_text_pdf_six_pages():
    read = []
    original = PageObject.extract_text

    def spy(self, *a, **kw):
        read.append(self.page_number)
        return original(self, *a, **kw)

    with patch.object(PageObject, "extract_text", spy):
        facts = probe(text_pdf(6))
    assert facts.has_text_layer and facts.language == "en" and facts.pages == 6
    assert 0 not in read and len(read) <= 4


def test_english_pdf_with_one_bamini_heading():
    facts = probe(text_pdf(4, legacy_lines=1, legacy_body="Ftpij"))
    assert facts.language == "en"
    assert 0 < facts.legacy_font_share < 0.3
    assert "Ftpij" not in facts.sample_text


def test_pdf_mostly_in_bamini():
    facts = probe(text_pdf(4, legacy_lines=12, legacy_body="mwptpay; mwpTk; jkpo; nkhop " * 2))
    assert (facts.language, facts.language_source) == ("ta", "legacy font")
    assert facts.legacy_font_share >= 0.3 and facts.language_confidence <= 0.95


def test_scanned_pdf_with_fake_detector():
    detector = FakeDetector()
    facts = probe(scanned_pdf(6), image_script_detector=detector)
    assert facts.scanned
    assert (facts.language, facts.language_source) == ("ta", "ocr script detection")
    assert detector.calls <= 2


def test_unreadable_scan_without_pdftoppm():
    detector = FakeDetector()
    with patch("shutil.which", return_value=None):
        facts = probe(image_xobject_pdf(800, 1000), image_script_detector=detector)
    assert facts.language is None and facts.language_source == "unreadable scan"
    assert detector.calls == 0


def test_detector_slower_than_the_budget():
    detector = FakeDetector(sleep=3)
    start = time.monotonic()
    facts = probe(scanned_pdf(3), image_script_detector=detector, scan_time_budget_s=1.0)
    assert time.monotonic() - start < 1.5
    assert facts.language_source == "timed out"


def test_huge_declared_image_is_skipped_without_decoding():
    detector = FakeDetector()
    with patch.object(Prober, "_decode_pdf_image") as decode:
        facts = probe(image_xobject_pdf(20_000, 20_000), image_script_detector=detector)
    decode.assert_not_called()
    assert facts.language_source == "unreadable scan"


def test_facts_round_trip():
    from pipeline_router import FileFacts

    facts = probe(text_pdf(3))
    assert FileFacts.from_dict(facts.to_dict()) == facts
