import codecs

from pipeline_router import Prober, SafetyCaps, decode_text, dominant_language


def test_decode_utf8_utf16_cp1252():
    assert decode_text("Tamil தமிழ்".encode()).text == "Tamil தமிழ்"
    assert decode_text(codecs.BOM_UTF16_LE + "Hindi हिंदी".encode("utf-16-le")).text == "Hindi हिंदी"
    french = "Le café est très célèbre à Paris, où l'élève étudie. " * 5
    decoded = decode_text(french.encode("cp1252"))
    assert decoded.text == french
    assert not decoded.legacy_encoding_suspected


def test_tscii_like_bytes_are_suspected_legacy():
    # TSCII puts Tamil letters at 0xAB-0xFF; as cp1252 they read as Latin-1 letters.
    word = bytes([0xAB, 0xE8, 0xC1, 0xA6, 0xD5])
    data = b" ".join([word, bytes([0xB8, 0xC7, 0xDA]), bytes([0xCE, 0xF1, 0xE5, 0xAA])] * 40)
    decoded = decode_text(data)
    assert decoded.legacy_encoding_suspected
    facts = Prober(SafetyCaps()).probe(data, "txt")
    assert facts.language is None
    assert facts.language_source == "suspected legacy encoding"


def mix(a: str, b: str, a_share: float, letters: int = 400) -> str:
    """Text with about `a_share` of its letters from word a."""
    na = round(letters * a_share / len(a))
    nb = round(letters * (1 - a_share) / len(b))
    return " ".join([a] * na + [b] * nb)


TAMIL = "தமிழ்"  # 5 letters and marks
ENGLISH = "word"


def test_tamil_and_mixes():
    assert dominant_language(" ".join([TAMIL] * 20)).language == "ta"
    g = dominant_language(mix(TAMIL, ENGLISH, 0.35))
    assert (g.language, g.script) == ("ta", "Taml")
    assert dominant_language(mix(TAMIL, ENGLISH, 0.10)).language == "en"


def test_indic():
    g = dominant_language("हिंदी भारत की भाषा है " * 6)
    assert (g.language, g.approximate) == ("hi", True)
    assert dominant_language("ଓଡ଼ିଆ ଭାଷା ଏକ ସୁନ୍ଦର ଭାଷା " * 5).language == "or"
    assert dominant_language("සිංහල භාෂාව ලස්සනයි " * 6).language == "si"


def test_arabic_script():
    assert dominant_language("یہ ایک اردو جملہ ہے اور یہ اچھا ہے " * 4).language == "ur"
    assert dominant_language("این گربه در خانه است و آن گل زیبا است " * 4).language == "fa"
    assert dominant_language("هذه جملة عربية بسيطة جدا " * 5).language == "ar"


def test_other_scripts():
    g = dominant_language("Это простое русское предложение " * 4)
    assert (g.language, g.approximate) == ("ru", True)
    assert dominant_language("これは日本語の文章です。私は学生です。" * 4).language == "ja"
    assert dominant_language("这是一个简单的中文句子我们学习" * 5).language == "zh"
    assert dominant_language("이것은 한국어 문장입니다 " * 6).language == "ko"


def test_too_few_letters_and_unlisted_script():
    assert dominant_language("தமிழ் word").language is None
    assert dominant_language("ይህ የአማርኛ ዓረፍተ ነገር ነው " * 10).language is None
