"""Subject: the form first, detection only as a fallback (spec 5.5)."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class SubjectGuess:
    subject: str | None
    confidence: float
    source: str  # "form" | "user" | "classifier" | "below threshold" | "none"
    detected: str | None = None


class UnknownSubject(ValueError):
    code = "unknown_subject"

    def __init__(self, subject: str, allowed):
        self.message = f"Unknown subject '{subject}'. Choose one of: {', '.join(allowed)}."
        super().__init__(self.message)


class SubjectResolver:
    def __init__(self, classifier=None, min_confidence: float = 0.7):
        self.classifier = classifier
        self.min_confidence = min_confidence

    def resolve(self, form_subject, sample_text, allowed_subjects, source="form") -> SubjectGuess:
        if form_subject and form_subject.strip():
            subject = form_subject.strip().lower()
            if subject not in allowed_subjects:
                raise UnknownSubject(subject, allowed_subjects)
            return SubjectGuess(subject, 1.0, source)
        if self.classifier is None or not (sample_text or "").strip():
            return SubjectGuess(None, 0.0, "none")
        subject, confidence = self.classifier.classify(sample_text, tuple(allowed_subjects))
        if subject not in allowed_subjects:
            return SubjectGuess(None, confidence, "none")
        if confidence < self.min_confidence:
            return SubjectGuess(None, confidence, "below threshold", detected=subject)
        return SubjectGuess(subject, confidence, "classifier", detected=subject)


# Starter lists; grow them from real textbooks.
KEYWORDS = {
    "physics": ["physics", "velocity", "acceleration", "momentum", "newton", "force", "gravity",
                "kinetic", "friction", "voltage", "current", "resistance", "optics", "lens",
                "wavelength", "frequency", "thermodynamics", "magnetic", "electron",
                "இயற்பியல்", "விசை", "முடுக்கம்", "भौतिकी", "बल", "वेग"],
    "chemistry": ["chemistry", "molecule", "atom", "compound", "reaction", "acid", "base",
                  "oxidation", "reduction", "valency", "periodic", "isotope", "bond", "mole",
                  "catalyst", "solution", "organic", "வேதியியல்", "அணு", "தனிமம்", "रसायन",
                  "अम्ल", "परमाणु"],
    "biology": ["biology", "cell", "organism", "photosynthesis", "chlorophyll", "enzyme",
                "protein", "gene", "chromosome", "dna", "tissue", "respiration", "species",
                "evolution", "mitosis", "meiosis", "உயிரியல்", "செல்", "ஒளிச்சேர்க்கை",
                "जीव विज्ञान", "कोशिका", "प्रकाश संश्लेषण"],
    "botany": ["botany", "plant", "root", "stem", "leaf", "flower", "seed", "pollination",
               "xylem", "phloem", "தாவரவியல்", "தாவர", "वनस्पति"],
    "zoology": ["zoology", "animal", "vertebrate", "invertebrate", "mammal", "reptile",
                "amphibian", "விலங்கியல்", "விலங்கு", "प्राणी विज्ञान"],
    "mathematics": ["mathematics", "math", "equation", "algebra", "geometry", "triangle",
                    "theorem", "integral", "derivative", "matrix", "polynomial", "probability",
                    "trigonometry", "fraction", "கணிதம்", "சமன்பாடு", "गणित", "समीकरण"],
}  # fmt: skip


class KeywordClassifier:
    """Offline and free. Latin words match whole words (optional plural s),
    other scripts as plain substrings."""

    def __init__(self, keywords: dict | None = None, min_hits: int = 3):
        self.keywords = keywords or KEYWORDS
        self.min_hits = min_hits
        self._patterns = {s: [self._pattern(w) for w in ws] for s, ws in self.keywords.items()}

    @staticmethod
    def _pattern(word: str):
        if word.isascii():
            return re.compile(rf"\b{re.escape(word)}s?\b", re.I)
        return re.compile(re.escape(word))

    def classify(self, text: str, subjects) -> tuple[str | None, float]:
        hits = {s: sum(len(p.findall(text)) for p in self._patterns.get(s, ())) for s in subjects}
        total = sum(hits.values())
        if total < self.min_hits:
            return None, 0.0
        top = max(hits, key=hits.get)
        return top, hits[top] / total


class LLMClassifier:
    """`complete` is the existing Gemini, Groq or DeepSeek call, injected."""

    MAX_CHARS = 2500

    def __init__(self, complete: Callable[[str], str]):
        self.complete = complete

    def classify(self, text: str, subjects) -> tuple[str | None, float]:
        prompt = (
            "Which school subject is this text from? Pick exactly one of: "
            f"{', '.join(subjects)}; or \"mixed\" if it covers several, or \"unknown\". "
            'Reply with JSON only, like {"subject": "biology", "confidence": 0.9}.\n\n'
            f"Text:\n{text[: self.MAX_CHARS]}"
        )
        try:
            reply = self.complete(prompt)
            found = json.loads(re.search(r"\{.*?\}", reply, re.S).group(0))
            subject = str(found["subject"]).strip().lower()
            confidence = min(1.0, max(0.0, float(found.get("confidence", 0))))
        except Exception:
            return None, 0.0
        if subject not in subjects:  # also "mixed" and "unknown"
            return None, 0.0
        return subject, confidence
