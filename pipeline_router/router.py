"""Input split, match blocks, readers and validation (spec 5.6)."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .filetype import ACCEPTED_KINDS

MATCH_KEYS = ("input", "language", "subject", "scanned")
INPUTS = ("file", "text", "topic")


class ConfigError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("Invalid pipeline config:\n- " + "\n- ".join(self.problems))


class NoPipeline(Exception):
    MESSAGES = {
        "no_reader": "No pipeline that fits this request can read this file type. Try PDF or DOCX.",
        "not_allowed": "The pipelines that fit this request aren't enabled for your project.",
        "no_match": "No pipeline fits this request.",
    }

    def __init__(self, code: str, skipped=()):
        self.code = code
        self.message = self.MESSAGES[code]
        self.skipped = list(skipped)
        super().__init__(code, self.message)


@dataclass(frozen=True)
class PipelineSpec:
    name: str
    input: str  # from match["input"], default "file"
    match: dict = field(default_factory=dict)  # WITHOUT the input key
    priority: int = 0
    readers: frozenset = field(default_factory=frozenset)  # kinds it can read (file input only)
    config: dict = field(default_factory=dict, compare=False)
    source: str = ""

    @property
    def specificity(self) -> int:
        return len(self.match)

    def matches(self, facts: dict) -> bool:
        return all(facts.get(k) == v for k, v in self.match.items())


@dataclass(frozen=True)
class RouteInput:
    input: str
    kind: str | None  # file kind, for the reader check; None for text and topic
    language: str | None
    subject: str | None
    scanned: bool = False

    def as_facts(self) -> dict:  # matching works on this dict, never on the dataclass
        return {"language": self.language, "subject": self.subject, "scanned": self.scanned}


@dataclass(frozen=True)
class RouteResult:
    spec: PipelineSpec
    fitted: list  # every pipeline that matched, best first
    skipped: list  # [{"pipeline": "tamil_biology", "why": "no reader for pptx"}]


def load_pipelines(directory) -> list[PipelineSpec]:
    """Routing fields on top; everything else is the pipeline config, so the
    builder's exported YAML can be pasted below them unchanged."""
    specs, problems = [], []
    paths = sorted(Path(directory).glob("*.yaml")) + sorted(Path(directory).glob("*.yml"))
    for path in paths:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            problems.append(f"{path.name}: not valid YAML ({e})")
            continue
        if not isinstance(doc, dict):
            problems.append(f"{path.name}: must be a mapping")
            continue
        config = dict(doc)
        name = str(config.pop("name", path.stem))
        match = config.pop("match", None) or {}
        priority = config.pop("priority", 0)
        if not isinstance(match, dict):
            problems.append(f"{path.name}: match must be a mapping")
            continue
        if not isinstance(priority, int) or isinstance(priority, bool):
            problems.append(f"{path.name}: priority must be a whole number")
            continue
        match = dict(match)
        input_ = match.pop("input", "file")
        readers = config.get("readers") or {}
        if not isinstance(readers, dict):
            problems.append(f"{path.name}: readers must be a mapping of kind to reader")
            continue
        specs.append(PipelineSpec(name, input_, match, priority, frozenset(readers), config, str(path)))
    if problems:
        raise ConfigError(problems)
    return specs


def _describe(match: dict) -> str:
    return ", ".join(f"{k} {str(v).lower() if isinstance(v, bool) else v}" for k, v in match.items())


def validate(specs) -> list[str]:
    """Raises ConfigError listing all problems; returns warnings."""
    errors, warnings = [], []
    seen = set()
    for s in specs:
        if s.name in seen:
            errors.append(f"duplicate pipeline name '{s.name}'")
        seen.add(s.name)
        unknown = set(s.match) - set(MATCH_KEYS)
        if unknown:
            errors.append(
                f"{s.name}: unknown match key(s) {', '.join(sorted(unknown))}; "
                f"allowed: {', '.join(MATCH_KEYS)}"
            )
        if s.input not in INPUTS:
            errors.append(f"{s.name}: input '{s.input}' must be one of {', '.join(INPUTS)}")
        if "scanned" in s.match and not isinstance(s.match["scanned"], bool):
            errors.append(f"{s.name}: scanned must be true or false, not {s.match['scanned']!r}")
        if s.input == "file" and not s.readers:
            errors.append(f"{s.name}: a file pipeline needs readers (which kinds it can read)")
        if s.input in ("text", "topic") and "scanned" in s.match:
            warnings.append(
                f"{s.name}: {s.input} pipelines are never scanned, so matching on scanned has no use"
            )
    for i, a in enumerate(specs):
        for b in specs[i + 1 :]:
            if a.input != b.input or a.specificity != b.specificity or a.priority != b.priority:
                continue
            shared = set(a.match) & set(b.match)
            if any(a.match[k] != b.match[k] for k in shared):
                continue  # they can never fit the same request
            if a.match == b.match:
                errors.append(
                    f"{a.name} and {b.name} have identical matches ({_describe(a.match) or 'default'}) "
                    "and the same priority; remove one or give one a higher priority"
                )
                continue
            both = {**a.match, **b.match}
            errors.append(
                f"{a.name} and {b.name} both fit a {a.input} request with {_describe(both)}; "
                f"add a pipeline for exactly that case or give one a higher priority"
            )
    for mode in INPUTS:
        if not any(s.input == mode and not s.match for s in specs):
            warnings.append(f"no default {mode} pipeline (one with an empty match)")
    defaults = [s for s in specs if s.input == "file" and not s.match]
    if defaults:
        readable = frozenset().union(*(s.readers for s in defaults))
        missing = [k for k in ACCEPTED_KINDS if k not in readable]
        if missing:
            warnings.append(f"no default file pipeline can read: {', '.join(missing)}")
    if errors:
        raise ConfigError(errors)
    return warnings


class PipelineRouter:
    def __init__(self, specs):
        self.specs = list(specs)
        self.warnings = validate(self.specs)
        self._by_name = {s.name: s for s in self.specs}

    @classmethod
    def from_directory(cls, path) -> "PipelineRouter":
        return cls(load_pipelines(path))

    def get(self, name: str) -> PipelineSpec:
        return self._by_name[name]

    @property
    def names(self) -> frozenset:
        return frozenset(self._by_name)

    def choose(self, ri: RouteInput, allowed: frozenset | None = None) -> RouteResult:
        facts = ri.as_facts()
        fitting = sorted(
            (s for s in self.specs if s.input == ri.input and s.matches(facts)),
            key=lambda s: (s.specificity, s.priority),
            reverse=True,
        )
        skipped = []
        for spec in fitting:
            if allowed is not None and spec.name not in allowed:
                skipped.append({"pipeline": spec.name, "why": "not allowed for this project"})
            elif ri.input == "file" and ri.kind not in spec.readers:
                skipped.append({"pipeline": spec.name, "why": f"no reader for {ri.kind}"})
            else:
                return RouteResult(spec, [s.name for s in fitting], skipped)
        if not fitting:
            raise NoPipeline("no_match")
        if all(s["why"].startswith("no reader") for s in skipped):
            raise NoPipeline("no_reader", skipped)
        raise NoPipeline("not_allowed", skipped)
