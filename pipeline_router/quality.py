"""Page quality (flow v3 step 4.9): can this page be read well enough to make
a quiz from it?

Each page image gets a 0-100 score: 100 minus a penalty per problem. At or
above `ok` it's fine; from `reject` to `ok` the file is accepted with a
warning and the worker skips its bad pages; under `reject` the file is
refused. Text layers get a garbled-text check instead.

The thresholds were set on synthetic pages (clean, blurred, dark, noisy...).
shortcut: tune them from real uploads (log the metrics, compare with what
users accept) before relying on the warn/reject split.
"""

import io
import re
import unicodedata
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageFilter

WIDTH = 1000  # every measure is taken at this width, so scores don't depend on the scan size
A4_SHORT_SIDE_IN = 8.27
CID = re.compile(r"\(cid:\d+\)")


@dataclass(frozen=True)
class QualityRules:
    ok: int = 70
    reject: int = 40
    garbled_page: float = 0.30  # a text layer this garbled is read from its picture instead


@dataclass(frozen=True)
class PageQuality:
    score: int | None  # None for a blank page
    issues: tuple = ()
    metrics: dict = field(default_factory=dict, compare=False)


# (issue, metric, warn at, bad at, max penalty). Penalty grows linearly from warn to bad.
CHECKS = (
    ("low_resolution", "dpi", 150, 75, 50),
    ("blurry", "sharpness", 1.0, 0.25, 65),
    ("dark", "background", 150, 90, 65),
    ("low_contrast", "contrast", 90, 35, 65),
    ("noisy", "noise", 4.0, 10.0, 35),
    ("glare", "glare", 0.04, 0.20, 30),
    ("cut_off", "edge_ink", 0.05, 0.25, 20),
    ("skewed", "skew", 3, 8, 15),
)
MESSAGES = {
    "low_resolution": "the resolution is too low",
    "blurry": "it is blurry",
    "dark": "it is too dark",
    "low_contrast": "the text is faint",
    "noisy": "it is grainy",
    "glare": "there is glare on the page",
    "cut_off": "the text is cut off at the edge",
    "skewed": "the page is tilted",
    "garbled": "the text in it is garbled",
    "blank": "the pages are blank",
}
BLANK_INK = 0.0002  # under 0.02% ink: an empty page, left out of the file's score
REJECTED = (
    "The uploaded file is not good enough quality ({}). Scan it again at 300 dpi in good light, "
    "or upload a clearer copy."
)


def _penalty(value, warn, bad, max_penalty) -> float:
    span = (value - warn) / (bad - warn)  # 0 at warn, 1 at bad; works for both directions
    return max_penalty * min(1.0, max(0.0, span))


def measure(img: Image.Image, page_width_in: float | None = None) -> dict:
    """The raw measures of one page image."""
    dpi = img.info.get("dpi", (0, 0))[0] if page_width_in is None else img.width / page_width_in
    if not dpi or dpi < 50:  # no usable DPI recorded: assume the short side is an A4 page
        dpi = min(img.size) / A4_SHORT_SIDE_IN
    g = img.convert("L")
    if g.width != WIDTH:
        g = g.resize((WIDTH, max(1, round(g.height * WIDTH / g.width))), Image.LANCZOS)
    a = np.asarray(g, dtype=np.float32)
    background = float(np.median(a))
    flat = np.sort(a, axis=None)
    ink = float(flat[: max(50, flat.size // 5000)].mean())  # the darkest few: the ink, however little
    contrast = background - ink
    ink_mask = a < (background + ink) / 2
    inner = a[1:-1, 1:-1]
    lap = np.abs(4 * inner - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:])
    top = max(100, int(ink_mask.sum()) // 10)  # the strongest edges, in proportion to the ink
    sharpness = float(np.partition(lap, lap.size - top, axis=None)[-top:].mean()) / max(contrast, 1.0)
    median = np.asarray(g.filter(ImageFilter.MedianFilter(3)), dtype=np.float32)[1:-1, 1:-1]
    paper = inner > background - 10
    noise = float(np.mean(np.abs(inner - median)[paper])) if paper.any() else 0.0
    return {
        "ink": round(float(ink_mask.mean()) if contrast >= 25 else 0.0, 4),
        "dpi": round(float(dpi)),
        "sharpness": round(sharpness, 3),
        "background": round(background),
        "contrast": round(contrast),
        "noise": round(noise, 2),
        "glare": round(_glare(a, background), 3),
        "edge_ink": round(_edge_ink(ink_mask), 3),
        "skew": _skew(ink_mask),
    }


def _glare(a, background) -> float:
    """Share of the page that is blown-out white on a darker (photographed) page."""
    if background >= 215:  # a scan: white paper is supposed to be white
        return 0.0
    h, w = (a.shape[0] // 16) * 16, (a.shape[1] // 16) * 16
    cells = a[:h, :w].reshape(16, h // 16, 16, w // 16)
    means, stds = cells.mean(axis=(1, 3)), cells.std(axis=(1, 3))
    return float(np.mean((means >= 250) & (stds < 4)))


def _edge_ink(ink) -> float:
    """Ink in the outer 2% band of the most crowded side. A side that is
    nearly all dark is a scanner border, not cut-off text."""
    band = max(2, ink.shape[1] // 50)
    sides = (ink[:, :band], ink[:, -band:], ink[:band, :], ink[-band:, :])
    return max((float(s.mean()) for s in sides if s.mean() < 0.8), default=0.0)


def _skew(ink) -> int:
    """Degrees of tilt, from the rotation that lines the text rows up best."""
    if ink.mean() < 0.002:
        return 0
    small = Image.fromarray((ink * 255).astype(np.uint8)).resize(
        (600, max(1, round(600 * ink.shape[0] / ink.shape[1])))
    )

    def rows(angle):
        return float(np.var(np.asarray(small.rotate(angle, fillcolor=0), dtype=np.float32).sum(axis=1)))

    return abs(max(range(-8, 9), key=rows))


def score(metrics: dict) -> PageQuality:
    if metrics["ink"] < BLANK_INK:
        return PageQuality(None, ("blank",), metrics)
    total, issues = 0.0, []
    for issue, name, warn, bad, max_penalty in CHECKS:
        penalty = _penalty(metrics[name], warn, bad, max_penalty)
        if penalty > 0:
            issues.append(issue)
            total += penalty
    return PageQuality(max(0, round(100 - total)), tuple(issues), metrics)


def image_quality(image, page_width_in: float | None = None) -> PageQuality:
    """`image` is PNG/JPEG bytes or a PIL image."""
    if isinstance(image, (bytes, bytearray)):
        with Image.open(io.BytesIO(image)) as img:
            img.load()
            return score(measure(img, page_width_in))
    return score(measure(image, page_width_in))


def file_quality(pages: list[PageQuality], rules: QualityRules = QualityRules()) -> tuple[int | None, tuple]:
    """The file's score (mean of its non-blank sampled pages) and the issues of
    the pages that weren't fine. All blank scores 0."""
    if not pages:
        return None, ()
    scored = [p for p in pages if p.score is not None]
    if not scored:
        return 0, ("blank",)
    issues = sorted({i for p in scored if p.score < rules.ok for i in p.issues})
    return round(sum(p.score for p in scored) / len(scored)), tuple(issues)


def rejection_message(issues) -> str:
    reasons = [MESSAGES[i] for i in issues if i in MESSAGES] or ["it can't be read clearly"]
    return REJECTED.format(", ".join(reasons))


def garbled_share(text: str) -> float:
    """Share of the visible characters that are extraction garbage: (cid:N)
    codes, replacement and private-use characters, controls, and UTF-8 read
    as cp1252 (Ã©, â€™)."""
    visible = sum(1 for c in text if not c.isspace())
    if not visible:
        return 0.0
    bad = sum(len(m) for m in CID.findall(text))
    bad += sum(1 for c in text if c == "�" or unicodedata.category(c) in ("Co", "Cc") and not c.isspace())
    bad += 2 * sum(text.count(m) for m in ("Ã", "â€", "Â"))
    return min(1.0, bad / visible)
