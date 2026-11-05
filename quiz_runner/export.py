"""Finished quizzes in other formats (flow step 15, export): CSV for
spreadsheets, GIFT for Moodle. JSON is the job body itself."""

import csv
import io
import re

LETTERS = "ABCDEFGH"
GIFT_SPECIAL = re.compile(r"([~=#{}:\\])")


def _answer_text(q: dict) -> str:
    kind = q["type"]
    if kind in ("mcq_single", "assertion_reason"):
        return f"{LETTERS[q['answer']]}. {q['options'][q['answer']]}"
    if kind == "mcq_multi":
        return "; ".join(f"{LETTERS[i]}. {q['options'][i]}" for i in q["answers"])
    if kind == "true_false":
        return "True" if q["answer"] else "False"
    if kind == "match":
        return "; ".join(f"{a} -> {b}" for a, b in q["pairs"])
    return str(q["answer"])


def to_csv(questions: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["number", "type", "difficulty", "question", "options", "answer", "explanation", "source_pages"]
    )
    for n, q in enumerate(questions, 1):
        options = " | ".join(f"{LETTERS[i]}. {o}" for i, o in enumerate(q.get("options") or ()))
        pages = " ".join(str(p) for p in q.get("source_pages") or ())
        writer.writerow([n, q["type"], q["difficulty"], q["question"], options, _answer_text(q),
                         q.get("explanation", ""), pages])  # fmt: skip
    return out.getvalue()


def _g(text: str) -> str:
    """GIFT escapes ~ = # { } : and backslash; formulas keep their $...$."""
    return GIFT_SPECIAL.sub(r"\\\1", text).replace("\n", " ")


def _feedback(q) -> str:
    return f" ####{_g(q['explanation'])}" if q.get("explanation") else ""


def gift_question(q: dict, n: int) -> str:
    title, kind = f"::Q{n} ({q['difficulty']})::", q["type"]
    text = _g(q["question"])
    if kind in ("mcq_single", "assertion_reason"):
        choices = " ".join(("=" if i == q["answer"] else "~") + _g(o) for i, o in enumerate(q["options"]))
        return f"{title}{text} {{{choices}{_feedback(q)}}}"
    if kind == "mcq_multi":
        right, wrong = len(q["answers"]), len(q["options"]) - len(q["answers"])
        weights = {True: f"%{100 / right:.5g}%", False: f"%-{100 / wrong:.5g}%"}
        choices = " ".join(f"~{weights[i in q['answers']]}{_g(o)}" for i, o in enumerate(q["options"]))
        return f"{title}{text} {{{choices}{_feedback(q)}}}"
    if kind == "true_false":
        return f"{title}{text} {{{'TRUE' if q['answer'] else 'FALSE'}{_feedback(q)}}}"
    if kind == "fill_blank":
        blank = f"{{={_g(q['answer'])}{_feedback(q)}}}"
        return title + re.sub(r"_{3,}", lambda m: blank, text, count=1)
    if kind == "match":
        pairs = " ".join(f"={_g(a)} -> {_g(b)}" for a, b in q["pairs"])
        return f"{title}{text} {{{pairs}{_feedback(q)}}}"
    raise ValueError(f"no GIFT form for {kind}")


def to_gift(questions: list[dict]) -> str:
    return "\n\n".join(gift_question(q, n) for n, q in enumerate(questions, 1)) + "\n"
