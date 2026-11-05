"""Text files in any common encoding (spec 5.2)."""

import codecs
from dataclasses import dataclass

import charset_normalizer

_BOMS = (  # UTF-32 first: its LE BOM starts with the UTF-16 LE one
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    legacy_encoding_suspected: bool


def _decode(data: bytes, encoding: str) -> str | None:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as e:
        # A character cut off by the 8 KB sniff sample is not binary.
        if e.reason in ("unexpected end of data", "truncated data") and e.start >= len(data) - 4:
            return _decode(data[: e.start], encoding)
        return None


def decode_text(data: bytes) -> DecodedText | None:
    """None means the bytes are binary."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            text = _decode(data[len(bom) :], encoding)
            return None if text is None else DecodedText(text, encoding, False)
    text = _decode(data, "utf-8")
    if text is not None:
        return DecodedText(text, "utf-8", False)
    results = charset_normalizer.from_bytes(data)
    best = results.best()
    if best is None:
        return None
    for m in results:  # exact ties are common on short text: prefer cp1252, like browsers
        if m.encoding == "cp1252" and (m.chaos, m.coherence) == (best.chaos, best.coherence):
            best = m
    text = str(best)
    return DecodedText(text, best.encoding, len(text) == len(data) and _legacy_suspected(data))


def _legacy_suspected(data: bytes) -> bool:
    """Single-byte text whose letters, read as latin-1, are at least 25% in
    U+0080-U+00FF: how TSCII Tamil looks. Judged on the bytes, because
    charset-normalizer may call TSCII Thai (cp874) or Cyrillic. Real Russian
    or Thai in an old code page is flagged too (language None, default
    pipeline): the bytes can't tell them apart."""
    letters = [c for c in data.decode("latin-1") if c.isalpha()]
    if not letters:
        return False
    high = sum(1 for c in letters if 0x80 <= ord(c) <= 0xFF)
    return high / len(letters) >= 0.25
