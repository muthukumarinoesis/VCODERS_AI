"""Word equations (OMML) to LaTeX, for KaTeX.

Covers what textbooks use: fractions, scripts, roots, sums and integrals,
brackets, functions, accents, bars, limits, matrices and equation arrays.
Anything else keeps its text, so nothing is silently lost.
"""

M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

NARY = {"∑": r"\sum", "∏": r"\prod", "∐": r"\coprod", "∫": r"\int", "∬": r"\iint", "∭": r"\iiint",
        "∮": r"\oint", "⋃": r"\bigcup", "⋂": r"\bigcap"}  # fmt: skip
ACCENT = {"\u0302": r"\hat", "\u0303": r"\tilde", "\u0304": r"\bar", "\u0305": r"\bar",
          "\u20d7": r"\vec", "\u0307": r"\dot", "\u0308": r"\ddot", "\u030c": r"\check",
          "\u0301": r"\acute", "\u0300": r"\grave"}  # fmt: skip
FUNCTIONS = {"sin", "cos", "tan", "cot", "sec", "csc", "log", "ln", "exp", "lim", "max", "min", "sinh",
             "cosh", "tanh", "arcsin", "arccos", "arctan", "det", "gcd", "deg"}  # fmt: skip
BRACKETS = {"(": "(", ")": ")", "[": "[", "]": "]", "{": r"\{", "}": r"\}", "|": "|", "‖": r"\|",
            "⟨": r"\langle", "⟩": r"\rangle", "⌊": r"\lfloor", "⌋": r"\rfloor", "⌈": r"\lceil",
            "⌉": r"\rceil", "": "."}  # fmt: skip
ESCAPE = {"\\": r"\backslash ", "{": r"\{", "}": r"\}", "%": r"\%", "#": r"\#", "&": r"\&", "$": r"\$",
          "_": r"\_", "^": r"\^{}", "~": r"\sim "}  # fmt: skip


def _prop(el, path: str, default=None):
    """The m:val of a property element such as m:naryPr/m:chr."""
    node = el
    for part in path.split("/"):
        node = node.find(M + part) if node is not None else None
    if node is None:
        return default
    return node.get(M + "val", default)


def _child(el, name):
    return el.find(M + name)


def _arg(el, name) -> str:
    c = _child(el, name)
    return convert(c) if c is not None else ""


def _text(value: str) -> str:
    return "".join(ESCAPE.get(c, c) for c in value)


def convert(el) -> str:
    """LaTeX for one OMML element and everything inside it."""
    tag = el.tag
    if tag == M + "r":
        text = "".join(t.text or "" for t in el.iter(M + "t"))
        normal = _child(el, "rPr") is not None and _child(_child(el, "rPr"), "nor") is not None
        return rf"\text{{{text}}}" if normal and text.strip() else _text(text)
    if tag == M + "f":
        num, den = _arg(el, "num"), _arg(el, "den")
        if _prop(el, "fPr/type") == "lin":
            return f"{{{num}}}/{{{den}}}"
        return rf"\frac{{{num}}}{{{den}}}"
    if tag == M + "sSup":
        return f"{{{_arg(el, 'e')}}}^{{{_arg(el, 'sup')}}}"
    if tag == M + "sSub":
        return f"{{{_arg(el, 'e')}}}_{{{_arg(el, 'sub')}}}"
    if tag == M + "sSubSup":
        return f"{{{_arg(el, 'e')}}}_{{{_arg(el, 'sub')}}}^{{{_arg(el, 'sup')}}}"
    if tag == M + "sPre":
        return f"{{}}_{{{_arg(el, 'sub')}}}^{{{_arg(el, 'sup')}}}{{{_arg(el, 'e')}}}"
    if tag == M + "rad":
        deg = _arg(el, "deg")
        if _prop(el, "radPr/degHide") in ("1", "on", "true") or not deg:
            return rf"\sqrt{{{_arg(el, 'e')}}}"
        return rf"\sqrt[{deg}]{{{_arg(el, 'e')}}}"
    if tag == M + "nary":
        op = NARY.get(_prop(el, "naryPr/chr", "∫"), _text(_prop(el, "naryPr/chr", "∫")))
        sub, sup = _arg(el, "sub"), _arg(el, "sup")
        return op + (f"_{{{sub}}}" if sub else "") + (f"^{{{sup}}}" if sup else "") + f"{{{_arg(el, 'e')}}}"
    if tag == M + "d":
        beg, end = _prop(el, "dPr/begChr", "("), _prop(el, "dPr/endChr", ")")
        sep = _prop(el, "dPr/sepChr", "|")
        parts = [convert(e) for e in el.findall(M + "e")]
        sep = r" \mid " if sep == "|" else f" {_text(sep)} "
        return rf"\left{BRACKETS.get(beg, _text(beg))}{sep.join(parts)}\right{BRACKETS.get(end, _text(end))}"
    if tag == M + "func":
        name = (
            "".join(t.text or "" for t in _child(el, "fName").iter(M + "t"))
            if _child(el, "fName") is not None
            else ""
        )
        head = rf"\{name}" if name in FUNCTIONS else rf"\operatorname{{{_text(name)}}}"
        return f"{head}{{{_arg(el, 'e')}}}"
    if tag == M + "acc":
        accent = ACCENT.get(_prop(el, "accPr/chr", "\u0302"), r"\hat")
        return f"{accent}{{{_arg(el, 'e')}}}"
    if tag == M + "bar":
        line = r"\underline" if _prop(el, "barPr/pos") == "bot" else r"\overline"
        return f"{line}{{{_arg(el, 'e')}}}"
    if tag in (M + "limLow", M + "limUpp"):
        base, lim = _arg(el, "e"), _arg(el, "lim")
        if base.strip() in (r"\lim", "lim"):
            return rf"\lim_{{{lim}}}"
        return rf"\underset{{{lim}}}{{{base}}}" if tag == M + "limLow" else rf"\overset{{{lim}}}{{{base}}}"
    if tag == M + "groupChr":
        return _arg(el, "e")
    if tag == M + "m":
        rows = [" & ".join(convert(e) for e in mr.findall(M + "e")) for mr in el.findall(M + "mr")]
        return r"\begin{matrix}" + r" \\ ".join(rows) + r"\end{matrix}"
    if tag == M + "eqArr":
        return r"\begin{aligned}" + r" \\ ".join(convert(e) for e in el.findall(M + "e")) + r"\end{aligned}"
    if tag.endswith("Pr") or tag in (M + "ctrlPr",):
        return ""  # properties: no content
    if tag == W + "r":  # Word text inside an equation
        return _text("".join(t.text or "" for t in el.iter(W + "t")))
    return "".join(convert(c) for c in el)  # oMath, e, num, den, sub, sup, box, phant, ...


def paragraph_text(p) -> str:
    """A Word paragraph's text with its equations as $...$ (inline) or
    $$...$$ (display), in reading order."""
    out = []
    for child in p:
        if child.tag == M + "oMathPara":
            out.append(" ".join(f"$${convert(m)}$$" for m in child.iter(M + "oMath")))
        elif child.tag == M + "oMath":
            out.append(f"${convert(child)}$")
        elif child.tag.endswith("}pPr"):
            continue
        elif child.find(f".//{M}oMath") is not None:  # equations inside hyperlinks, insertions...
            out.append(paragraph_text(child))
        else:
            out.append("".join(t.text or "" for t in child.iter(W + "t")))
    return "".join(out)
