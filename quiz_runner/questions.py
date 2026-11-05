"""What a valid question looks like, and the KaTeX check for its formulas."""

import json
import re
import shutil
import subprocess
from pathlib import Path

SUPPORTED_TYPES = ("mcq_single", "mcq_multi", "true_false", "assertion_reason", "fill_blank", "match")
LEVELS = ("easy", "medium", "hard")
AR_OPTIONS = (
    "Both A and R are true, and R is the correct explanation of A.",
    "Both A and R are true, but R is not the correct explanation of A.",
    "A is true, but R is false.",
    "A is false, but R is true.",
)
FORMATS = {
    "mcq_single": '{"question": "...", "options": ["...", "...", "...", "..."], "answer": <index 0-3 of the '
    'correct option>, "explanation": "..."}',
    "mcq_multi": '{"question": "...", "options": [4 to 6 strings], "answers": [indexes of every correct '
    'option, at least one, not all], "explanation": "..."}',
    "true_false": '{"question": "<a statement>", "answer": true or false, "explanation": "..."}',
    "assertion_reason": '{"assertion": "...", "reason": "...", "answer": "A" | "B" | "C" | "D", '
    '"explanation": "..."} where A = both true and R explains A; B = both true, R does not explain A; '
    "C = A true, R false; D = A false, R true",
    "fill_blank": '{"question": "<a sentence with ____ for the missing words>", "answer": "<the missing '
    'words>", "explanation": "..."}',
    "match": '{"question": "<instruction>", "pairs": [["<left>", "<right>"], ... 3 to 8 pairs], '
    '"explanation": "..."}',
}
MATH = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$|\\\[(.+?)\\\]|\\\((.+?)\\\)", re.S)
HERE = Path(__file__).parent / "katex_check"


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _index(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def normalize(q, allowed_types) -> tuple[dict | None, str | None]:
    """(question, None) when valid, else (None, why). Strings are trimmed."""
    if not isinstance(q, dict):
        return None, "not an object"
    kind, level = q.get("type"), q.get("difficulty")
    if kind not in allowed_types:
        return None, f"type {kind!r} not allowed"
    if level not in LEVELS:
        return None, f"difficulty {level!r}"
    out = {"type": kind, "difficulty": level, "explanation": _text(q.get("explanation"))}
    question = _text(q.get("question"))
    if kind in ("mcq_single", "mcq_multi"):
        options = q.get("options")
        size = (4, 4) if kind == "mcq_single" else (4, 6)
        if not isinstance(options, list) or not size[0] <= len(options) <= size[1]:
            return None, "wrong number of options"
        options = [_text(o) for o in options]
        if not all(options) or len({o.casefold() for o in options}) != len(options):
            return None, "empty or repeated options"
        if kind == "mcq_single":
            answer = _index(q.get("answer"))
            if answer is None or not 0 <= answer < len(options):
                return None, "answer is not an option index"
            out["answer"] = answer
        else:
            answers = q.get("answers")
            if not isinstance(answers, list) or any(_index(a) is None for a in answers):
                return None, "answers must be option indexes"
            answers = sorted(set(answers))
            if not answers or len(answers) == len(options) or not all(0 <= a < len(options) for a in answers):
                return None, "answers must be some, not all, of the options"
            out["answers"] = answers
        out.update(question=question, options=options)
    elif kind == "true_false":
        if not isinstance(q.get("answer"), bool):
            return None, "answer must be true or false"
        out.update(question=question, answer=q["answer"])
    elif kind == "assertion_reason":
        assertion, reason = _text(q.get("assertion")), _text(q.get("reason"))
        if not assertion or not reason:
            return None, "assertion and reason are required"
        if q.get("answer") not in ("A", "B", "C", "D"):
            return None, "answer must be A, B, C or D"
        question = f"Assertion (A): {assertion}\nReason (R): {reason}"
        out.update(question=question, assertion=assertion, reason=reason, options=list(AR_OPTIONS),
                   answer="ABCD".index(q["answer"]))  # fmt: skip
    elif kind == "fill_blank":
        answer = _text(q.get("answer"))
        if "___" not in question or not answer:
            return None, "needs a ____ blank and an answer"
        out.update(question=question, answer=answer)
    elif kind == "match":
        pairs = q.get("pairs")
        if not isinstance(pairs, list) or not 3 <= len(pairs) <= 8:
            return None, "needs 3 to 8 pairs"
        if not all(isinstance(p, list) and len(p) == 2 for p in pairs):
            return None, "each pair is [left, right]"
        pairs = [[_text(a), _text(b)] for a, b in pairs]
        lefts, rights = [a for a, _ in pairs], [b for _, b in pairs]
        if not all(lefts + rights) or len(set(lefts)) != len(lefts) or len(set(rights)) != len(rights):
            return None, "empty or repeated pair items"
        out.update(question=question or "Match the items.", pairs=pairs)
    if not out["question"]:
        return None, "no question text"
    return out, None


def strings_of(q: dict):
    for value in q.values():
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    yield item
                elif isinstance(item, list):
                    yield from (s for s in item if isinstance(s, str))


def math_spans(text: str) -> list[tuple[str, bool]]:
    """(tex, display) for every $...$, $$...$$, \\(...\\) and \\[...\\]."""
    return [(m.group(2) or m.group(4) or m.group(1) or m.group(3), bool(m.group(1) or m.group(3)))
            for m in MATH.finditer(text)]  # fmt: skip


class KatexChecker:
    """Formulas checked with the real KaTeX (throwOnError), in one node call per batch."""

    def __init__(self, timeout_s: float = 30):
        self.node = shutil.which("node")
        self.timeout_s = timeout_s

    def available(self) -> bool:
        return self.node is not None and (HERE / "node_modules" / "katex").is_dir()

    def errors(self, spans: list[tuple[str, bool]]) -> list[str | None]:
        if not spans:
            return []
        payload = json.dumps([{"tex": t, "display": d} for t, d in spans])
        done = subprocess.run(
            [self.node, str(HERE / "check.js")], input=payload, capture_output=True, text=True,
            timeout=self.timeout_s, check=True, cwd=HERE,
        )  # fmt: skip
        return json.loads(done.stdout)
