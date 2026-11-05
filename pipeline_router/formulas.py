"""Does this text or page carry formulas? (flow v3: formula detection)

Text layers mangle formulas (x² becomes "x2", fractions fall apart), so a
formula-heavy PDF page is better read from its picture, where the reader
writes KaTeX. The probe reports it for pricing; the worker acts on it.
"""

import re
import unicodedata

MATH_FONT = re.compile(
    r"cmmi|cmsy|cmex|msam|msbm|cambria ?math|stix.*math|latin ?modern ?math|xits|asana|mt ?extra|"
    r"mathematicalpi|euclid|symbolmt|^symbol$|lmmath|math",
    re.I,
)
LATEX = re.compile(
    r"\\(?:frac|sqrt|int|sum|prod|lim|alpha|beta|theta|pi|infty|cdot|times|le|ge|neq|vec)\b|[_^]\{"
)
GREEK = (0x0391, 0x03C9)
SUPER_SUB = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎"
FORMULA_SHARE = 0.03  # math characters per visible character: a page "has formulas" above this
MIN_LATEX = 3


def math_share(text: str) -> float:
    """Share of visible characters that are math: operators and symbols (Sm),
    Greek letters, super/subscript digits, plus LaTeX commands."""
    visible = sum(1 for c in text if not c.isspace())
    if not visible:
        return 0.0
    math = sum(1 for c in text if _is_math_char(c))
    math += 4 * len(LATEX.findall(text))
    return min(1.0, math / visible)


def _is_math_char(c: str) -> bool:
    if unicodedata.category(c) == "Sm" and c not in "+-=<>|~":  # plain-text operators don't count
        return True
    return GREEK[0] <= ord(c) <= GREEK[1] or c in SUPER_SUB


def has_formulas(text: str, math_font_share: float = 0.0) -> bool:
    return (
        math_font_share >= FORMULA_SHARE
        or math_share(text) >= FORMULA_SHARE
        or len(LATEX.findall(text)) >= MIN_LATEX
    )


def is_math_font(name: str) -> bool:
    return bool(MATH_FONT.search(re.sub(r"^[A-Z]{6}\+", "", name or "")))
