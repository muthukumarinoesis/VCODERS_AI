from fixtures import ENGLISH, HINDI, TAMIL, docx_bytes, odf_bytes, page_photo, pptx_bytes, xlsx_bytes
from olegen import make_ole, word_stream

from pipeline_router import Prober, SafetyCaps, sniff


def facts_of(data, hint=None, **kw):
    caps = SafetyCaps()
    return Prober(caps, **kw).probe(data, sniff(data, caps, hint))


def test_pptx_with_tamil_slides():
    f = facts_of(pptx_bytes([TAMIL * 2] * 6))
    assert (f.kind, f.language, f.pages) == ("pptx", "ta", 6)


def test_xlsx_with_english_cells():
    f = facts_of(xlsx_bytes([[w for w in ENGLISH.split()[i : i + 4]] for i in range(0, 24, 4)] * 5))
    assert (f.kind, f.language) == ("xlsx", "en")


def test_xlsx_without_dimension_record_counts_rows_seen():
    f = facts_of(xlsx_bytes([["row", i] for i in range(120)], strip_dimension=True))
    # sampling stops at 200 cells = 100 rows seen; 100 / 50
    assert f.pages == 2


def test_odt_in_hindi():
    f = facts_of(odf_bytes("text", HINDI * 2))
    assert (f.kind, f.language) == ("odt", "hi")


def test_rtf_with_unicode_escapes():
    escaped = "".join(f"\\u{ord(c)}?" if ord(c) > 127 else c for c in TAMIL * 2)
    f = facts_of(("{\\rtf1\\ansi{\\fonttbl{\\f0 Latha;}}\\f0 " + escaped + "\\par}").encode())
    assert (f.kind, f.language) == ("rtf", "ta")


def test_docx_and_doc():
    assert facts_of(docx_bytes([TAMIL * 3])).language == "ta"
    f = facts_of(make_ole({"WordDocument": word_stream(text=TAMIL * 3)}))
    assert (f.kind, f.language) == ("doc", "ta")


def test_html_drops_script_and_style():
    page = (
        "<!doctype html><html><head><style>body{color:red}</style>"
        "<script>var x = 'английский текст ' .repeat(50)</script></head><body><p>"
        + TAMIL * 2 + "</p></body></html>"
    )  # fmt: skip
    f = facts_of(page.encode())
    assert (f.kind, f.language) == ("html", "ta")
    assert "color" not in f.sample_text


def test_long_document_sample_text():
    data = (("Opening words. " + ENGLISH * 200) + " FINALWORD").encode()
    f = facts_of(data)
    assert len(f.sample_text) <= 2000
    assert "FINALWORD" in f.sample_text and "Opening" in f.sample_text


def test_image_file_is_a_scan():
    class Detector:
        def detect(self, image, timeout_s):
            return ("Deva", 0.8)

    f = facts_of(page_photo(), image_script_detector=Detector())
    assert f.scanned and (f.language, f.language_source) == ("hi", "ocr script detection")
    assert f.language_approximate


def test_pasted_text_and_topics():
    prober = Prober(SafetyCaps())
    assert prober.text_facts(TAMIL * 3).language == "ta"
    topic = prober.topic_facts("Photosynthesis", "TA")
    assert (topic.kind, topic.pages, topic.language, topic.language_source) == (None, 0, "ta", "request")
    assert prober.topic_facts("Photosynthesis").language is None  # under 50 characters
    assert prober.topic_facts(TAMIL * 2).language_source == "text"


def test_old_office_text_scan_ignores_misread_bytes():
    from pipeline_router.probe import ole_text

    english_8bit = b"\x00\x01" + b"The cell is the basic unit of life" + b"\x00\x02"
    assert "The cell is the basic unit of life" in ole_text(english_8bit)
    assert not any("㐀" <= c <= "鿿" for c in ole_text(english_8bit))  # no CJK from ASCII pairs
    assert ole_text(b"\x05" + b"\xff" * 40 + b"\xcc\xff\xff\xff\xd8" * 8) == ""  # binary filler
    tamil, chinese = "தமிழ் ஒரு மொழி", "这是一个简单的中文句子"
    assert tamil in ole_text(b"\x01\x00" + tamil.encode("utf-16-le") + b"\x00\x00")
    assert chinese in ole_text(b"\x01" + chinese.encode("utf-16-le") + b"\x00")  # odd alignment still read
    assert "photosynthesis" in ole_text("photosynthesis".encode("utf-16-le"))


def test_ppt_text_records():
    import struct

    from pipeline_router.probe import ppt_text

    def atom(rtype, body):
        return struct.pack("<HHI", 0x0000, rtype, len(body)) + body

    slide = (
        atom(0x0FA0, "தமிழ் பாடம்".encode("utf-16-le"))
        + atom(0x0FA8, b"Cells and tissues")
        + atom(0x1234, b"\x9f\x88")
    )
    stream = struct.pack("<HHI", 0x000F, 0x03E8, len(slide)) + slide + b"\x01\x02"  # a container, then junk
    assert ppt_text(stream) == "தமிழ் பாடம்\nCells and tissues"
