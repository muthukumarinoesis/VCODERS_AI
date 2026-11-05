import json
from unittest.mock import Mock

import pytest
from fixtures import ENGLISH, TAMIL, text_pdf

from pipeline_router import (
    FileFacts,
    FileRefused,
    KeywordClassifier,
    PipelineRouter,
    Prober,
    ProjectConfig,
    RoutingService,
    SafetyCaps,
    SubjectResolver,
    load_projects,
)

SUBJECTS = ("physics", "chemistry", "biology", "mathematics")
QUICKTEST = ProjectConfig("quicktest", None, SUBJECTS, 20)


@pytest.fixture
def service(pipelines_dir):
    caps = SafetyCaps()
    router = PipelineRouter.from_directory(pipelines_dir)
    return RoutingService(router, Prober(caps), SubjectResolver(KeywordClassifier()), caps)


def test_english_pdf_no_subject(service):
    d = service.decide_file(text_pdf(3, body="Some plain words about nothing in particular. " * 4), QUICKTEST)
    assert d.pipeline == "default"
    assert d.reason == "default is the default file pipeline; nothing more specific fitted"
    json.dumps(d.to_dict())


def test_project_cap_message_has_both_sizes(service):
    with pytest.raises(FileRefused) as e:
        service.decide_file(b"%PDF-" + b"\x00" * (25 * 2**20), QUICKTEST)
    assert e.value.code == "too_large"
    assert "25.0 MB" in e.value.message and "20 MB" in e.value.message


def test_force_language(service):
    d = service.decide_file(text_pdf(3), QUICKTEST, force_language="ta", form_subject="biology")
    assert (d.language, d.language_source, d.pipeline) == ("ta", "user", "tamil_biology")
    assert d.reason == "tamil_biology is the most specific file pipeline for language ta, subject biology"


def test_skipped_reason_wording(service):
    facts = FileFacts("pptx", 32, True, 1.0, "ta", "Taml", 0.61, False, "text")
    d = service.route(facts, "file", QUICKTEST, form_subject="biology")
    assert d.reason == (
        "tamil_default is the most specific file pipeline for language ta; "
        "skipped tamil_biology (no reader for pptx)"
    )


def test_correction_never_inspects_again(service):
    stored = service.decide_file(text_pdf(3), QUICKTEST)
    facts = service.inspect(text_pdf(3))
    service.prober = Mock(side_effect=AssertionError("inspected again"))
    d = service.route(FileFacts.from_dict(facts.to_dict()), "file", QUICKTEST,
                      force_language="ta", force_subject="Biology")  # fmt: skip
    assert (d.pipeline, d.subject, d.subject_source) == ("tamil_biology", "biology", "user")
    assert stored.pipeline != d.pipeline
    assert not service.prober.mock_calls


def test_text_and_topic_modes(service):
    assert service.decide_text(TAMIL * 3, QUICKTEST, form_subject="biology").pipeline == "tamil_text"
    d = service.decide_topic("Photosynthesis", QUICKTEST, form_subject="biology", language="ta")
    assert (d.pipeline, d.language, d.language_source, d.kind) == ("topic_bank", "ta", "request", None)


def test_pasted_text_over_cap(service):
    with pytest.raises(FileRefused) as e:
        service.decide_text("a" * (501 * 1024), QUICKTEST)
    assert e.value.code == "too_large"


def test_subject_detected_from_text(service):
    d = service.decide_text(ENGLISH * 3, QUICKTEST)
    assert (d.subject, d.subject_source) == ("biology", "classifier")


def test_estimate(service):
    facts = service.inspect(text_pdf(3))
    assert service.estimate(facts, 10).reading == "text"


def test_projects_file(pipelines_dir, tmp_path):
    router = PipelineRouter.from_directory(pipelines_dir)
    projects = load_projects(pipelines_dir.parent / "projects.yaml", router)
    assert projects["quicktest"].allowed is None and projects["quickans"].allowed == {
        "text_default",
        "topic_bank",
        "default",
    }
    bad = tmp_path / "p.yaml"
    bad.write_text("x:\n  allowed: [nope]\n  subjects: [Biology]\n  max_file_mb: 80\n  limits: {jobs: 1}\n")
    from pipeline_router import ConfigError

    with pytest.raises(ConfigError) as e:
        load_projects(bad, router)
    assert len(e.value.problems) == 4


def test_inspect_job_in_a_child_process():
    import concurrent.futures as cf
    import multiprocessing

    from pipeline_router import inspect_job

    # spawn: arguments, result and FileRefused must really pickle
    with cf.ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as pool:
        facts = pool.submit(inspect_job, text_pdf(2), None, None).result(timeout=30)
        assert facts.kind == "pdf"
        with pytest.raises(FileRefused) as e:
            pool.submit(inspect_job, b"", None, None).result(timeout=30)
    assert e.value.code == "empty_file"


def test_dotenv_loader(tmp_path, monkeypatch):
    from examples.context import load_dotenv

    (tmp_path / ".env").write_text(
        '# comment\nNEW_ONE="hello # not a comment"\n\nKEPT=from-file\nnot a line\nDIR=var/x    # note\nEMPTY=   # c\n'
    )
    for name in ("DIR", "EMPTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEPT", "from-env")
    monkeypatch.delenv("NEW_ONE", raising=False)
    load_dotenv(tmp_path / ".env")
    import os

    assert os.environ["NEW_ONE"] == "hello # not a comment" and os.environ["KEPT"] == "from-env"
    assert os.environ["DIR"] == "var/x" and os.environ["EMPTY"] == ""
    for name in ("NEW_ONE", "DIR", "EMPTY"):
        monkeypatch.delenv(name)
    load_dotenv(tmp_path / "missing.env")  # no file: nothing happens
