"""The calls the API makes (spec 5.9)."""

import dataclasses
from dataclasses import dataclass

from .caps import MB, SafetyCaps
from .filetype import FileRefused, sniff
from .probe import FileFacts
from .router import RouteInput


@dataclass(frozen=True)
class Estimate:
    pages: int
    scanned: bool
    reading: str  # "text" | "ocr" | "vision" (scans and legacy encodings need more than text)
    question_count: int | None


@dataclass(frozen=True)
class Decision:
    project: str
    pipeline: str
    input: str
    kind: str | None
    pages: int
    scanned: bool
    language: str | None
    language_source: str
    language_confidence: float
    language_approximate: bool
    legacy_font_share: float
    subject: str | None
    subject_source: str
    subject_confidence: float
    subject_detected: str | None
    fitted: list
    skipped: list
    reason: str
    probe_ms: int
    quality_score: int | None = None  # 40-69: accepted, but tell the user (quality_issues says why)
    quality_issues: tuple = ()
    formulas: bool = False

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _reason(spec, input_: str, skipped) -> str:
    if not spec.match:
        reason = f"{spec.name} is the default {input_} pipeline; nothing more specific fitted"
    else:
        what = ", ".join(f"{k} {str(v).lower() if isinstance(v, bool) else v}" for k, v in spec.match.items())
        reason = f"{spec.name} is the most specific {input_} pipeline for {what}"
    if skipped:
        reason += "; skipped " + ", ".join(f"{s['pipeline']} ({s['why']})" for s in skipped)
    return reason


class RoutingService:
    def __init__(self, router, prober, subjects, caps: SafetyCaps):
        self.router = router
        self.prober = prober
        self.subjects = subjects
        self.caps = caps

    # Heavy: runs in the inspection child. Arguments and result are picklable.
    def inspect(self, data: bytes, max_file_mb: float | None = None,
                filename_hint: str | None = None) -> FileFacts:  # fmt: skip
        limit = min(max_file_mb or self.caps.max_file_mb, self.caps.max_file_mb)
        if len(data) > limit * MB:
            raise FileRefused(
                "too_large",
                f"This file is {len(data) / MB:.1f} MB; the limit is {limit:g} MB. "
                "Upload a smaller file or split it.",
            )
        kind = sniff(data, self.caps, filename_hint)
        return self.prober.probe(data, kind)

    # Cheap: runs in the API process.
    def route(self, facts: FileFacts, input: str, project, form_subject: str | None = None,
              force_language: str | None = None, force_subject: str | None = None) -> Decision:  # fmt: skip
        if force_subject:
            guess = self.subjects.resolve(force_subject, "", project.subjects, source="user")
        else:
            guess = self.subjects.resolve(form_subject, facts.sample_text, project.subjects)
        language, source = facts.language, facts.language_source
        confidence, approximate = facts.language_confidence, facts.language_approximate
        if force_language:
            language, source = force_language.strip().lower(), "user"
            confidence, approximate = 1.0, False
        scanned = facts.scanned if input == "file" else False
        result = self.router.choose(
            RouteInput(input, facts.kind if input == "file" else None, language, guess.subject, scanned),
            allowed=project.allowed,
        )
        return Decision(
            project=project.key,
            pipeline=result.spec.name,
            input=input,
            kind=facts.kind,
            pages=facts.pages,
            scanned=scanned,
            language=language,
            language_source=source,
            language_confidence=confidence,
            language_approximate=approximate,
            legacy_font_share=facts.legacy_font_share,
            subject=guess.subject,
            subject_source=guess.source,
            subject_confidence=guess.confidence,
            subject_detected=guess.detected,
            fitted=result.fitted,
            skipped=result.skipped,
            reason=_reason(result.spec, input, result.skipped),
            probe_ms=facts.probe_ms,
            quality_score=facts.quality_score,
            quality_issues=facts.quality_issues,
            formulas=facts.formulas,
        )

    def estimate(self, facts: FileFacts, question_count: int | None = None) -> Estimate:
        if facts.scanned:
            reading = "ocr"
        elif facts.language_source in ("legacy font", "suspected legacy encoding"):
            reading = "vision"
        elif facts.formulas and facts.kind == "pdf":
            reading = "vision"  # formula pages are read from their pictures
        else:
            reading = "text"
        return Estimate(facts.pages, facts.scanned, reading, question_count)

    # Conveniences for tests and simple callers: inspect + route, without isolation.
    def decide_file(self, data, project, form_subject=None, filename_hint=None,
                    force_language=None, force_subject=None) -> Decision:  # fmt: skip
        facts = self.inspect(data, project.max_file_mb, filename_hint)
        return self.route(facts, "file", project, form_subject, force_language, force_subject)

    def decide_text(self, text, project, form_subject=None, force_language=None,
                    force_subject=None) -> Decision:  # fmt: skip
        facts = self.prober.text_facts(text)
        return self.route(facts, "text", project, form_subject, force_language, force_subject)

    def decide_topic(self, topic, project, form_subject=None, language=None) -> Decision:
        facts = self.prober.topic_facts(topic, language)
        return self.route(facts, "topic", project, form_subject)
