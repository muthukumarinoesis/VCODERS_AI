"""The flow steps added after the first build: formulas, verify, safety,
focus (retrieve), duplicate uploads, export and migrations."""

import csv
import io
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from fake_embedder import FakeEmbedder
from fake_llm import FakeLLM, make_question
from fastapi.testclient import TestClient
from fixtures import ENGLISH, docx_bytes, pdf_of, text_pdf
from lxml import etree
from svc import ThreadPool, make_ctx
from test_runner import big_pdf, config, job
from test_worker import add_job

from examples import db, worker
from examples.fastapi_app import create_app
from examples.quiz_options import BadOptions
from examples.quiz_options import validate as validate_options
from pipeline_router import Prober, SafetyCaps, sniff
from pipeline_router.formulas import has_formulas, is_math_font, math_share
from quiz_runner import KatexChecker, QuizRunner, RunnerError, read
from quiz_runner.export import gift_question, to_csv, to_gift
from quiz_runner.omml import convert, paragraph_text
from quiz_runner.runner import parse_verdicts

MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
FORMULAS = "Find ∫ x² dx and ∑ αᵢ for θ ≤ π; √2 ≈ 1.41, ∞ > β ≥ γ. " * 8


def inspect(data):
    caps = SafetyCaps()
    return Prober(caps).probe(data, sniff(data, caps))


# ---- formulas --------------------------------------------------------------------


def test_formula_detection():
    assert has_formulas(FORMULAS) and math_share(FORMULAS) > 0.03
    assert has_formulas(r"Use \frac{a}{b}, then \sqrt{x} and x^{2} again.")
    assert not has_formulas(ENGLISH * 3) and not has_formulas("தமிழ் ஒரு மொழி " * 10)
    assert is_math_font("ABCDEF+CMMI10") and is_math_font("Cambria Math") and not is_math_font("Helvetica")


def om(inner: str):
    return etree.fromstring(f'<m:oMath xmlns:m="{MATH_NS}">{inner}</m:oMath>')


def r(text):
    return f"<m:r><m:t>{text}</m:t></m:r>"


OMML_CASES = [
    (f"<m:f><m:num>{r('a')}</m:num><m:den>{r('b')}</m:den></m:f>", r"\frac{a}{b}"),
    (f"<m:sSup><m:e>{r('x')}</m:e><m:sup>{r('2')}</m:sup></m:sSup>", "{x}^{2}"),
    (f"<m:sSub><m:e>{r('H')}</m:e><m:sub>{r('2')}</m:sub></m:sSub>{r('O')}", "{H}_{2}O"),
    (f"<m:rad><m:radPr><m:degHide m:val='1'/></m:radPr><m:deg/><m:e>{r('x')}</m:e></m:rad>", r"\sqrt{x}"),
    (f"<m:rad><m:deg>{r('3')}</m:deg><m:e>{r('8')}</m:e></m:rad>", r"\sqrt[3]{8}"),
    (f"<m:nary><m:naryPr><m:chr m:val='∑'/></m:naryPr><m:sub>{r('i=1')}</m:sub><m:sup>{r('n')}</m:sup>"
     f"<m:e>{r('i')}</m:e></m:nary>", r"\sum_{i=1}^{n}{i}"),
    (f"<m:nary><m:sub>{r('0')}</m:sub><m:sup>{r('1')}</m:sup><m:e>{r('x dx')}</m:e></m:nary>", r"\int_{0}^{1}{x dx}"),
    (f"<m:d><m:e>{r('a+b')}</m:e></m:d>", r"\left(a+b\right)"),
    (f"<m:d><m:dPr><m:begChr m:val='['/><m:endChr m:val=']'/></m:dPr><m:e>{r('0')}</m:e><m:e>{r('1')}</m:e></m:d>",
     r"\left[0 \mid 1\right]"),
    (f"<m:func><m:fName>{r('sin')}</m:fName><m:e>{r('θ')}</m:e></m:func>", r"\sin{θ}"),
    (f"<m:acc><m:accPr><m:chr m:val='⃗'/></m:accPr><m:e>{r('v')}</m:e></m:acc>", r"\vec{v}"),
    (f"<m:bar><m:e>{r('x')}</m:e></m:bar>", r"\overline{x}"),
    (f"<m:limLow><m:e>{r('lim')}</m:e><m:lim>{r('x→0')}</m:lim></m:limLow>", r"\lim_{x→0}"),
    (f"<m:m><m:mr><m:e>{r('1')}</m:e><m:e>{r('0')}</m:e></m:mr><m:mr><m:e>{r('0')}</m:e><m:e>{r('1')}</m:e></m:mr></m:m>",
     r"\begin{matrix}1 & 0 \\ 0 & 1\end{matrix}"),
    (f"<m:eqArr><m:e>{r('x=1')}</m:e><m:e>{r('y=2')}</m:e></m:eqArr>", r"\begin{aligned}x=1 \\ y=2\end{aligned}"),
    (r("50% of {x}"), r"50\% of \{x\}"),
]  # fmt: skip


@pytest.mark.parametrize("inner,latex", OMML_CASES)
def test_word_equations_become_latex(inner, latex):
    assert convert(om(inner)) == latex


def test_every_converted_equation_is_valid_katex():
    checker = KatexChecker()
    assert checker.errors([(latex, False) for _, latex in OMML_CASES]) == [None] * len(OMML_CASES)


def test_docx_with_an_equation():
    import docx

    d = docx.Document()
    p = d.add_paragraph("Simplify ")
    p._p.append(om(f"<m:f><m:num>{r('a')}</m:num><m:den>{r('b')}</m:den></m:f>"))
    p._p.append(etree.fromstring(
        '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:t> now.</w:t></w:r>'))  # fmt: skip
    display = d.add_paragraph()
    display._p.append(
        etree.fromstring(
            f'<m:oMathPara xmlns:m="{MATH_NS}">{etree.tostring(om(r("E=mc²"))).decode()}</m:oMathPara>'
        )
    )
    d.add_paragraph(ENGLISH * 3)
    buf = io.BytesIO()
    d.save(buf)
    data = buf.getvalue()
    text = read("docx", data, {"docx": {"impl": "python_docx"}})[0].text
    assert r"Simplify $\frac{a}{b}$ now." in text and "$$E=mc²$$" in text
    assert inspect(data).formulas and not inspect(docx_bytes([ENGLISH * 3])).formulas
    assert (
        paragraph_text(
            etree.fromstring('<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        )
        == ""
    )


def test_formula_pdf_is_read_from_pictures():
    data = pdf_of([FORMULAS] * 3)
    facts = inspect(data)
    assert facts.formulas and not facts.scanned
    from pipeline_router import RoutingService

    assert RoutingService(None, None, None, SafetyCaps()).estimate(facts).reading == "vision"
    pages = read("pdf", data, {"pdf": {"impl": "pymupdf"}})
    assert all(p.needs_ocr and p.text for p in pages)  # text kept as a fallback
    assert not any(p.needs_ocr for p in read("pdf", data, {"pdf": {"impl": "pymupdf", "formulas": "text"}}))
    assert not any(p.needs_ocr for p in read("pdf", big_pdf(2), {"pdf": {"impl": "pymupdf"}}))


def test_math_font_pdf_counts_as_formulas():
    data = text_pdf(4, legacy_lines=12, legacy_body="x y z a b c d e f g h", legacy_font="CMMI10")
    assert inspect(data).formulas
    assert any(p.needs_ocr for p in read("pdf", data, {"pdf": {"impl": "pymupdf"}}))


def test_formula_pages_not_read_from_pictures_keep_their_text():
    llm = FakeLLM(transcript=ENGLISH * 5)
    cfg = config(2, reading={"max_ocr_pages": 1})
    QuizRunner(llm)(cfg, job(), pdf_of([FORMULAS] * 3))
    material = llm.prompts[0][0]
    assert len(llm.image_calls) == 1 and "∫" in material  # the other pages still contribute text


# ---- verify --------------------------------------------------------------------------


def test_questions_that_fail_the_check_are_replaced():
    llm = FakeLLM(reject=lambda q: q["question"].startswith(("Question 1:", "Question 3:")))
    result = QuizRunner(llm)(config(6), job(), big_pdf(4))
    assert result["made"] == 6 and result["unverified"] == 0
    assert all(q["verified"] for q in result["questions"])
    assert not any(q["question"].startswith(("Question 1:", "Question 3:")) for q in result["questions"])
    assert len(llm.verify_prompts) >= 2 and "<material>" in llm.verify_prompts[0]


def test_check_unavailable_keeps_questions_unverified_and_unbanked(tmp_path):
    from examples.bank import QuestionBank

    bank = QuestionBank(make_ctx(tmp_path).engine)
    llm = FakeLLM(verify_fails=100)
    result = QuizRunner(llm, embedder=FakeEmbedder(), bank=bank)(config(3), job(), big_pdf(2))
    assert result["made"] == 3 and result["unverified"] == 3
    (v,) = FakeEmbedder().embed(["material item"], "query")
    assert bank.find(None, None, None, ["mcq_single"], v, 10, 0.0) == []


def test_check_can_be_turned_off():
    llm = FakeLLM()
    QuizRunner(llm)(config(3, verify=False), job(), big_pdf(2))
    assert llm.verify_prompts == []


def test_verdict_parsing():
    assert parse_verdicts('```json\n{"results": [{"n": 1, "ok": true}, {"n": 3, "ok": "yes"}]}\n```', 3) == [
        True, False, False]  # fmt: skip


# ---- AI safety ----------------------------------------------------------------------


def _gemini(response):
    from quiz_runner import GeminiClient

    with patch("google.genai.Client"):
        client = GeminiClient("key")
    client.client.models.generate_content.return_value = response
    return client


def test_blocked_prompt_or_answer_is_unsafe_content():
    blocked = _gemini(MagicMock(text="", prompt_feedback=MagicMock(block_reason="SAFETY"), candidates=[]))
    with pytest.raises(RunnerError) as e:
        blocked.complete("p")
    assert e.value.code == "unsafe_content"
    reason = MagicMock()
    reason.name = "SAFETY"
    stopped = _gemini(MagicMock(text="", prompt_feedback=None, candidates=[MagicMock(finish_reason=reason)]))
    with pytest.raises(RunnerError):
        stopped.complete("p")
    ok = MagicMock()
    ok.name = "STOP"
    assert (
        _gemini(
            MagicMock(text="fine", prompt_feedback=None, candidates=[MagicMock(finish_reason=ok)])
        ).complete("p")
        == "fine"
    )


def test_unsafe_content_fails_the_job_without_retrying():
    class Unsafe(FakeLLM):
        def complete(self, prompt, **kw):
            self.prompts.append((prompt, None))
            raise RunnerError("unsafe_content", "flagged")

    llm = Unsafe()
    with pytest.raises(RunnerError) as e:
        QuizRunner(llm)(config(2), job(), big_pdf(2))
    assert e.value.code == "unsafe_content" and len(llm.prompts) == 1


# ---- focus (retrieve) ---------------------------------------------------------------------

VOLCANO = "Volcanoes erupt lava and ash. Magma rises through the crust of the earth. " * 40
PLANTS = "Photosynthesis makes glucose in leaves using chlorophyll and sunlight energy. " * 40


@pytest.mark.parametrize("embedder", [FakeEmbedder(), None])
def test_focus_uses_only_the_matching_parts(embedder):
    llm = FakeLLM()
    cfg = config(4)
    cfg["quiz"]["focus"] = "photosynthesis in leaves"
    QuizRunner(llm, embedder=embedder)(cfg, job(), pdf_of([VOLCANO, VOLCANO, PLANTS, VOLCANO]))
    for prompt, _ in llm.prompts:
        assert "Photosynthesis" in prompt and "Volcanoes" not in prompt and "Ask only about" in prompt


def test_focus_option_rules():
    assert validate_options({"count": 5, "focus": "  chapter  3 "}, {})["focus"] == "chapter 3"
    with pytest.raises(BadOptions):
        validate_options({"count": 5, "focus": "x" * 201}, {})


# ---- duplicate uploads and export (API) --------------------------------------------------


@pytest.fixture
def api(tmp_path):
    ctx = make_ctx(tmp_path, runner=QuizRunner(FakeLLM(), katex=KatexChecker()))
    calls = []

    def counting_inspect(data, mb, hint):
        calls.append(1)
        return ctx.routing.inspect(data, mb, hint)

    client = TestClient(create_app(ctx, pool=ThreadPool(), inspect=counting_inspect))
    return ctx, client, calls


def test_same_file_twice_is_inspected_once(api):
    ctx, client, calls = api
    data = big_pdf(2)
    first = client.post("/v1/route", headers={"X-API-Key": ctx.key}, files={"file": ("a.pdf", data)}).json()
    second = client.post("/v1/route", headers={"X-API-Key": ctx.key}, files={"file": ("b.pdf", data)}).json()
    assert first["duplicate_of"] is None and second["duplicate_of"] == first["upload_id"]
    assert second["upload_id"] != first["upload_id"] and len(calls) == 1
    assert second["decision"]["pipeline"] == first["decision"]["pipeline"]
    other = client.post(
        "/v1/route", headers={"X-API-Key": ctx.other_key}, files={"file": ("a.pdf", data)}
    ).json()
    assert other["duplicate_of"] is None and len(calls) == 2  # never across projects


def _finished_job(ctx):
    job_id = add_job(ctx, "default", decision={"pipeline": "default", "input": "file", "kind": "pdf"},
                     options={"count": 6, "types": ["mcq_single", "true_false", "fill_blank"],
                              "difficulty": {"easy": 1.0}})  # fmt: skip
    worker.run_job(ctx, job_id)
    return job_id


def test_export_formats(api):
    ctx, client, _ = api
    job_id = _finished_job(ctx)
    h = {"X-API-Key": ctx.key}
    r = client.get(f"/v1/jobs/{job_id}/export?format=csv", headers=h)
    assert (
        r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    )
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 6 and rows[0]["answer"] and rows[0]["source_pages"]
    gift = client.get(f"/v1/jobs/{job_id}/export?format=gift", headers=h).text
    import re

    assert len(re.findall(r"^::Q\d+ ", gift, re.M)) == 6 and "{TRUE" in gift and "{=cells" in gift
    assert client.get(f"/v1/jobs/{job_id}/export", headers=h).json()["status"] == "succeeded"
    assert client.get(f"/v1/jobs/{job_id}/export?format=pdf", headers=h).status_code == 400
    assert client.get(f"/v1/jobs/{job_id}/export", headers={"X-API-Key": ctx.other_key}).status_code == 404
    queued = add_job(ctx)
    assert client.get(f"/v1/jobs/{queued}/export", headers=h).json()["error"] == "not_ready"


def test_gift_for_every_type_and_escaping():
    from quiz_runner import normalize

    out = {}
    for kind in ("mcq_single", "mcq_multi", "true_false", "assertion_reason", "fill_blank", "match"):
        q, _ = normalize(make_question(kind, "easy", 1), [kind])
        out[kind] = gift_question(q, 1)
    assert "=B1" in out["mcq_single"] and "~%50%A1" in out["mcq_multi"] and "~%-50%B1" in out["mcq_multi"]
    assert (
        "=L10 -> R10" in out["match"]
        and "{=cells" in out["fill_blank"]
        and "=Both A and R" in out["assertion_reason"]
    )
    q, _ = normalize({**make_question("mcq_single", "easy", 2), "question": "Is 2 = 2? {yes} #tag a:b ~x"},
                     ["mcq_single"])  # fmt: skip
    assert r"Is 2 \= 2? \{yes\} \#tag a\:b \~x" in gift_question(q, 1)
    assert to_gift([q]).endswith("\n") and to_csv([q]).splitlines()[0].startswith("number,type")


# ---- migrations (real Postgres only) --------------------------------------------------------


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL (Postgres)")
def test_migrations_build_exactly_the_code_schema():
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine, text

    url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(url)
    db.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), db.metadata)
    assert diff == []
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine.dispose()


def test_job_body_json_is_plain(api):
    ctx, client, _ = api
    job_id = _finished_job(ctx)
    body = client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": ctx.key}).json()
    assert json.loads(json.dumps(body))["quiz"]["made"] == 6
