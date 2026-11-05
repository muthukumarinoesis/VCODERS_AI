import json
from unittest.mock import MagicMock, patch

import pytest
from fake_embedder import FakeEmbedder
from fake_llm import FakeLLM, make_question
from svc import make_ctx
from test_runner import PIPELINES, big_pdf, config

from examples.bank import QuestionBank
from quiz_runner import GeminiEmbedder, QuizRunner
from quiz_runner.embeddings import question_text, unit

TOPIC = PIPELINES.get("topic_bank").config


@pytest.fixture
def bank(tmp_path):
    return QuestionBank(make_ctx(tmp_path).engine)


def topic_job(project="quicktest", language="ta", subject="biology", job_id="j1"):
    return {
        "id": job_id,
        "project": project,
        "pipeline": "topic_bank",
        "decision": {"input": "topic", "kind": None, "language": language, "subject": subject},
    }


def topic_config(count, **extra):
    return config(count, base=TOPIC, bank={"min_score": 0.2}, **extra)


def test_rewordings_are_dropped_as_duplicates():
    calls = []

    def reply(prompt):
        calls.append(prompt)
        if len(calls) == 1:  # the same question asked two ways
            same = make_question("mcq_single", "easy", 1)
            other = {**same, "question": "Question 1: which is the material about item 1?"}
            return json.dumps({"questions": [same, other]})
        return json.dumps({"questions": [make_question("mcq_single", "easy", 2)]})

    result = QuizRunner(FakeLLM(reply=reply), embedder=FakeEmbedder())(config(2), _file_job(), big_pdf(2))
    assert result["made"] == 2 and len(calls) == 2
    assert "Don't repeat" in calls[1]


def test_without_embeddings_rewordings_stay():
    def reply(prompt):
        same = make_question("mcq_single", "easy", 1)
        return json.dumps(
            {"questions": [same, {**same, "question": "Question 1: which is the material about item 1?"}]}
        )

    assert QuizRunner(FakeLLM(reply=reply))(config(2), _file_job(), big_pdf(2))["made"] == 2


def test_embedding_failure_still_makes_the_quiz():
    result = QuizRunner(FakeLLM(), embedder=FakeEmbedder(fail=True))(config(3), _file_job(), big_pdf(2))
    assert result["made"] == 3


def _file_job():
    return {"id": "j", "project": "quicktest", "pipeline": "default",
            "decision": {"input": "file", "kind": "pdf", "language": None}}  # fmt: skip


# ---- the bank ----------------------------------------------------------------


def _questions(n, about):
    return [make_question("mcq_single", "easy", i, f" about {about}") for i in range(1, n + 1)]


def test_bank_scopes_by_project_language_and_subject(bank):
    emb = FakeEmbedder()
    qs = _questions(3, "photosynthesis")
    bank.add(topic_job(), qs, emb.embed([question_text(q) for q in qs]))
    (v,) = emb.embed(["photosynthesis"], "query")
    base = dict(project="quicktest", language="ta", subject="biology", types=["mcq_single"])

    def find(**kw):
        return bank.find(**{**base, "vector": v, "limit": 10, "min_score": 0.2, **kw})

    assert len(find()) == 3 and find()[0][2] >= find()[-1][2]
    assert find(project="other") == [] and find(language="hi") == [] and find(subject="physics") == []
    assert len(find(subject=None)) == 3
    assert find(types=["true_false"]) == [] and find(min_score=0.99) == []


def test_topic_reuses_the_bank_before_generating(tmp_path):
    ctx = make_ctx(tmp_path)
    bank, emb = QuestionBank(ctx.engine), FakeEmbedder()
    first_llm = FakeLLM()
    first = QuizRunner(first_llm, embedder=emb, bank=bank)(topic_config(5), topic_job(), b"Photosynthesis")
    assert (first["made"], first["from_bank"]) == (5, 0)

    second_llm = FakeLLM()
    second = QuizRunner(second_llm, embedder=emb, bank=bank)(
        topic_config(5), topic_job(job_id="j2"), b"Photosynthesis"
    )
    assert (second["made"], second["from_bank"]) == (5, 5)
    assert second_llm.prompts == []  # nothing generated, nothing paid for
    with ctx.engine.connect() as conn:
        from sqlalchemy import func, select

        from examples import db

        assert conn.execute(select(func.count()).select_from(db.bank_questions)).scalar() == 5  # no re-saving


def test_partial_bank_hit_generates_the_rest_and_avoids_repeats(tmp_path):
    ctx = make_ctx(tmp_path)
    bank, emb = QuestionBank(ctx.engine), FakeEmbedder()
    QuizRunner(FakeLLM(), embedder=emb, bank=bank)(topic_config(3), topic_job(), b"Photosynthesis")
    llm = FakeLLM()
    result = QuizRunner(llm, embedder=emb, bank=bank)(
        topic_config(8), topic_job(job_id="j2"), b"Photosynthesis"
    )
    assert (result["made"], result["from_bank"]) == (8, 3)
    assert "Don't repeat" in llm.prompts[0][0]


def test_other_topics_projects_and_pipelines_dont_reuse(tmp_path):
    ctx = make_ctx(tmp_path)
    bank, emb = QuestionBank(ctx.engine), FakeEmbedder()
    QuizRunner(FakeLLM(), embedder=emb, bank=bank)(topic_config(3), topic_job(), b"Photosynthesis")
    run = lambda cfg, job, topic: QuizRunner(FakeLLM(), embedder=emb, bank=bank)(
        cfg, job, topic
    )  # noqa: E731
    assert run(topic_config(3), topic_job(job_id="a"), b"Volcanoes")["from_bank"] == 0
    assert run(topic_config(3), topic_job("other", job_id="b"), b"Photosynthesis")["from_bank"] == 0
    no_bank = config(3, base={"quiz": {}}, bank={"min_score": 0.2})  # no `sources: [question_bank]`
    assert run(no_bank, topic_job(job_id="c"), b"Photosynthesis")["from_bank"] == 0


def test_bank_failure_generates_everything(tmp_path):
    broken = MagicMock()
    broken.find.side_effect = ConnectionError("db down")
    result = QuizRunner(FakeLLM(), embedder=FakeEmbedder(), bank=broken)(
        topic_config(3), topic_job(), b"Cells"
    )
    assert (result["made"], result["from_bank"]) == (3, 0)


def test_file_questions_are_saved_for_the_project(tmp_path):
    ctx = make_ctx(tmp_path)
    bank, emb = QuestionBank(ctx.engine), FakeEmbedder()
    QuizRunner(FakeLLM(), embedder=emb, bank=bank)(config(4), _file_job(), big_pdf(2))
    (v,) = emb.embed(["material item"], "query")
    assert len(bank.find("quicktest", None, None, ["mcq_single"], v, 10, 0.0)) == 4


# ---- Gemini embeddings ----------------------------------------------------------


def test_gemini_embedder_request():
    with patch("google.genai.Client"):
        embedder = GeminiEmbedder("key")
        embedder.client.models.embed_content.side_effect = lambda **kw: MagicMock(
            embeddings=[MagicMock(values=[3.0, 4.0]) for _ in kw["contents"]]
        )
        vectors = embedder.embed([f"t{i}" for i in range(150)], "query")
    calls = embedder.client.models.embed_content.call_args_list
    assert [len(c.kwargs["contents"]) for c in calls] == [100, 50]
    cfg = calls[0].kwargs["config"]
    assert (calls[0].kwargs["model"], cfg.task_type, cfg.output_dimensionality) == (
        "gemini-embedding-001", "RETRIEVAL_QUERY", 768)  # fmt: skip
    assert vectors[0] == pytest.approx([0.6, 0.8]) and unit([0, 0]) == [0, 0]
