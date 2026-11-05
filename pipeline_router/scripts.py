"""Language from characters (spec 5.3).

Only Unicode letters and marks of the scripts below are counted; digits,
spaces and punctuation are ignored. Text in a script that isn't listed is
not counted at all, so it gets language None and the language-free
pipeline - this is intended.

Approximate languages: Devanagari is reported as hi (also Marathi, Nepali,
Sanskrit), Bengali as bn (also Assamese), Cyrillic as ru, and every Latin
script as en. Add a language model for those scripts if it matters.
"""

import unicodedata
from dataclasses import dataclass, field

# code, ranges, language, approximate
SCRIPTS = (
    ("Taml", ((0x0B80, 0x0BFF),), "ta", False),
    ("Deva", ((0x0900, 0x097F),), "hi", True),
    ("Beng", ((0x0980, 0x09FF),), "bn", True),
    ("Guru", ((0x0A00, 0x0A7F),), "pa", False),
    ("Gujr", ((0x0A80, 0x0AFF),), "gu", False),
    ("Orya", ((0x0B00, 0x0B7F),), "or", False),
    ("Telu", ((0x0C00, 0x0C7F),), "te", False),
    ("Knda", ((0x0C80, 0x0CFF),), "kn", False),
    ("Mlym", ((0x0D00, 0x0D7F),), "ml", False),
    ("Sinh", ((0x0D80, 0x0DFF),), "si", False),
    ("Arab", ((0x0600, 0x06FF), (0x0750, 0x077F)), "ar", True),
    ("Hebr", ((0x0590, 0x05FF),), "he", False),
    ("Grek", ((0x0370, 0x03FF),), "el", False),
    ("Cyrl", ((0x0400, 0x04FF),), "ru", True),
    ("Thai", ((0x0E00, 0x0E7F),), "th", False),
    ("Hang", ((0xAC00, 0xD7AF), (0x1100, 0x11FF)), "ko", False),
    ("Kana", ((0x3040, 0x30FF),), "ja", False),
    ("Hani", ((0x4E00, 0x9FFF),), "zh", True),
    ("Latn", ((0x0000, 0x024F),), "en", True),
)
LANGUAGE = {code: lang for code, _, lang, _ in SCRIPTS}
APPROXIMATE = {code: approx for code, _, _, approx in SCRIPTS}
SCRIPT_OF_LANGUAGE = {"ur": "Arab", "fa": "Arab", "ja": "Kana"}
SCRIPT_OF_LANGUAGE.update({lang: code for code, _, lang, _ in SCRIPTS})

URDU_LETTERS = set("ٹڈڑںے")
PERSIAN_LETTERS = set("پچژگی")
MIN_LETTERS = 50


@dataclass(frozen=True)
class LanguageGuess:
    language: str | None
    script: str | None
    confidence: float
    approximate: bool
    letters: int
    shares: dict = field(default_factory=dict)


def _script_of(ch: str) -> str | None:
    cp = ord(ch)
    for code, ranges, _, _ in SCRIPTS:
        if any(lo <= cp <= hi for lo, hi in ranges):
            return code
    return None


def count_scripts(text: str) -> dict:
    counts: dict = {}
    for ch in text:
        if unicodedata.category(ch)[0] not in "LM":
            continue
        code = _script_of(ch)
        if code:
            counts[code] = counts.get(code, 0) + 1
    return counts


def dominant_language(text: str, min_share: float = 0.30) -> LanguageGuess:
    counts = count_scripts(text)
    letters = sum(counts.values())
    if letters < MIN_LETTERS:
        return LanguageGuess(None, None, 0.0, False, letters, {})
    shares = {code: n / letters for code, n in counts.items()}
    # Han and kana are one CJK share; kana >= 5% of it means Japanese.
    groups = dict(counts)
    kana, han = groups.pop("Kana", 0), groups.pop("Hani", 0)
    if kana + han:
        groups["Kana" if kana >= 0.05 * (kana + han) else "Hani"] = kana + han
    non_latin = {c: n for c, n in groups.items() if c != "Latn"}
    top = max(non_latin, key=non_latin.get) if non_latin else None
    if top is None or non_latin[top] / letters < min_share:
        top = max(groups, key=groups.get)
    language = LANGUAGE[top]
    if top == "Arab":
        if URDU_LETTERS & set(text):
            language = "ur"
        elif PERSIAN_LETTERS & set(text):
            language = "fa"
    approximate = APPROXIMATE[top] and top != "Kana"
    return LanguageGuess(language, top, groups[top] / letters, approximate, letters, shares)
