import pytest

from pipeline_router import ConfigError, NoPipeline, PipelineRouter, PipelineSpec, RouteInput, load_pipelines

FILE_KINDS = frozenset({"pdf", "docx", "image"})


def spec(name, match=None, input="file", priority=0, readers=FILE_KINDS):
    return PipelineSpec(name, input, match or {}, priority, readers if input == "file" else frozenset())


def file_in(language=None, subject=None, kind="pdf", scanned=False):
    return RouteInput("file", kind, language, subject, scanned)


@pytest.fixture
def router(pipelines_dir):
    return PipelineRouter.from_directory(pipelines_dir)


def test_starter_set_loads_cleanly(router):
    assert router.warnings == []
    assert len(router.specs) == 7


def test_modes_never_cross(router):
    assert router.choose(RouteInput("text", None, "ta", "biology")).spec.name == "tamil_text"
    assert router.choose(RouteInput("topic", None, "ta", "biology")).spec.name == "topic_bank"


def test_tamil_biology_pdf(router):
    r = router.choose(file_in("ta", "biology"))
    assert r.spec.name == "tamil_biology"
    assert r.fitted == ["tamil_biology", "tamil_default", "default"]


def test_tamil_biology_pptx_skips_for_reader(router):
    r = router.choose(file_in("ta", "biology", kind="pptx"))
    assert r.spec.name == "tamil_default"
    assert r.skipped == [{"pipeline": "tamil_biology", "why": "no reader for pptx"}]


def test_language_routes(router):
    assert router.choose(file_in("ta", "physics")).spec.name == "tamil_default"
    assert router.choose(file_in("hi")).spec.name == "hindi_default"
    assert router.choose(file_in(None)).spec.name == "default"


def test_allowed_lists(router):
    r = router.choose(file_in("ta"), allowed=frozenset({"default"}))
    assert r.spec.name == "default"
    assert r.skipped == [{"pipeline": "tamil_default", "why": "not allowed for this project"}]
    with pytest.raises(NoPipeline) as e:
        router.choose(file_in("en"), allowed=frozenset({"tamil_biology"}))
    assert e.value.code == "not_allowed"


def test_no_reader_and_no_match():
    r = PipelineRouter([spec("default", readers=frozenset({"pdf"})), spec("t", {"input": "text"}, "text")])
    with pytest.raises(NoPipeline) as e:
        r.choose(file_in(kind="xlsx"))
    assert e.value.code == "no_reader"
    only_tamil = PipelineRouter([spec("ta", {"language": "ta"})])
    with pytest.raises(NoPipeline) as e:
        only_tamil.choose(file_in("en"))
    assert e.value.code == "no_match"


def test_ambiguous_pair_is_a_config_error():
    with pytest.raises(ConfigError) as e:
        PipelineRouter([
            spec("default"),
            spec("tamil_biology", {"language": "ta", "subject": "biology"}),
            spec("tamil_scans", {"language": "ta", "scanned": True}),
        ])  # fmt: skip
    msg = str(e.value)
    assert "tamil_biology" in msg and "tamil_scans" in msg and "higher priority" in msg


def test_priority_breaks_the_tie():
    r = PipelineRouter([
        spec("default"),
        spec("tamil_biology", {"language": "ta", "subject": "biology"}),
        spec("tamil_scans", {"language": "ta", "scanned": True}, priority=1),
    ])  # fmt: skip
    assert r.choose(file_in("ta", "biology", scanned=True)).spec.name == "tamil_scans"


def test_same_match_in_different_modes_loads():
    PipelineRouter([spec("f", {"language": "ta"}), spec("t", {"language": "ta"}, "text")])


def test_identical_matches_are_an_error():
    with pytest.raises(ConfigError, match="identical"):
        PipelineRouter([spec("a", {"language": "ta"}), spec("b", {"language": "ta"})])


def test_all_problems_in_one_error():
    with pytest.raises(ConfigError) as e:
        PipelineRouter([
            spec("dup"), spec("dup", {"language": "hi"}),
            spec("bad_key", {"lang": "ta"}),
            spec("bad_scanned", {"scanned": "yes"}),
            spec("no_readers", {"language": "ml"}, readers=frozenset()),
        ])  # fmt: skip
    problems = e.value.problems
    assert any("duplicate" in p for p in problems)
    assert any("lang" in p for p in problems)
    assert any("scanned must be true or false" in p for p in problems)
    assert any("needs readers" in p for p in problems)


def test_text_pipeline_on_scanned_warns():
    r = PipelineRouter([spec("default"), spec("t", input="text"), spec("ts", {"scanned": True}, "text"),
                        spec("topic", input="topic")])  # fmt: skip
    assert any("never scanned" in w for w in r.warnings)


def test_loader_takes_input_out_of_match(tmp_path):
    (tmp_path / "t.yaml").write_text("match: {input: text, language: ta}\nquiz: {count: 5}\n")
    (s,) = load_pipelines(tmp_path)
    assert (s.name, s.input, s.match, s.config) == ("t", "text", {"language": "ta"}, {"quiz": {"count": 5}})
