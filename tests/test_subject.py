import pytest

from pipeline_router import KeywordClassifier, LLMClassifier, SubjectResolver, UnknownSubject

SUBJECTS = ("physics", "chemistry", "biology", "mathematics")


def test_form_value():
    g = SubjectResolver().resolve(" Biology ", "", SUBJECTS)
    assert (g.subject, g.source, g.confidence) == ("biology", "form", 1.0)
    with pytest.raises(UnknownSubject):
        SubjectResolver().resolve("history", "", SUBJECTS)


def test_no_classifier_or_text():
    assert SubjectResolver().resolve(None, "cells", SUBJECTS).source == "none"
    assert SubjectResolver(KeywordClassifier()).resolve(None, "", SUBJECTS).source == "none"


def test_keyword_classifier():
    resolver = SubjectResolver(KeywordClassifier())
    bio = "The cell has chromosomes. Photosynthesis needs chlorophyll. Enzymes are proteins."
    g = resolver.resolve(None, bio, SUBJECTS)
    assert (g.subject, g.source) == ("biology", "classifier")
    mixed = "Velocity and acceleration of a force. Cells, genes and enzymes."
    g = resolver.resolve(None, mixed, SUBJECTS)
    assert (g.subject, g.source) == (None, "below threshold") and g.detected in ("physics", "biology")


def test_keyword_classifier_tamil_and_too_few_hits():
    assert (
        KeywordClassifier().classify("உயிரியல் பாடம்: செல் மற்றும் ஒளிச்சேர்க்கை", SUBJECTS)[0] == "biology"
    )
    assert KeywordClassifier().classify("a cell", SUBJECTS) == (None, 0.0)


def test_llm_classifier_replies():
    fenced = LLMClassifier(lambda p: '```json\n{"subject": "Biology", "confidence": 1.7}\n```')
    assert fenced.classify("text", SUBJECTS) == ("biology", 1.0)
    assert LLMClassifier(lambda p: '{"subject": "mixed", "confidence": 0.9}').classify("t", SUBJECTS) == (
        None,
        0.0,
    )
    assert LLMClassifier(lambda p: "no json here").classify("t", SUBJECTS) == (None, 0.0)

    def boom(prompt):
        raise TimeoutError

    assert LLMClassifier(boom).classify("t", SUBJECTS) == (None, 0.0)


def test_llm_prompt_is_capped():
    seen = []
    LLMClassifier(lambda p: seen.append(p) or "{}").classify("x" * 10_000, SUBJECTS)
    assert seen[0].count("x") <= 2500 + 5
