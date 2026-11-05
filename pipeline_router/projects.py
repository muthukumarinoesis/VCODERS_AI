"""Which client may use what (spec 5.8)."""

import re
from dataclasses import dataclass, field, fields

import yaml

from .caps import SafetyCaps
from .router import ConfigError

SUBJECT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class ProjectLimits:
    route_per_minute: int = 60
    jobs_per_minute: int = 20
    jobs_at_once: int = 10
    jobs_per_day: int = 2000
    pages_per_day: int = 50000


@dataclass(frozen=True)
class ProjectConfig:
    key: str
    allowed: frozenset | None  # None means every pipeline
    subjects: tuple = ()
    max_file_mb: float | None = None
    webhook: str | None = None
    limits: ProjectLimits = field(default_factory=ProjectLimits)


def load_projects(path, router, caps: SafetyCaps | None = None) -> dict[str, ProjectConfig]:
    caps = caps or SafetyCaps()
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ConfigError([f"{path}: must be a mapping of project name to settings"])
    projects, problems = {}, []
    limit_names = {f.name for f in fields(ProjectLimits)}
    for key, cfg in doc.items():
        cfg = cfg or {}
        allowed = cfg.get("allowed", "*")
        if allowed == "*":
            allowed = None
        else:
            allowed = frozenset(allowed or ())
            for name in sorted(allowed - router.names):
                problems.append(f"{key}: allowed pipeline '{name}' doesn't exist")
        subjects = tuple(cfg.get("subjects") or ())
        for s in subjects:
            if not isinstance(s, str) or not SUBJECT_RE.match(s):
                problems.append(f"{key}: subject {s!r} must be a lowercase identifier")
        max_mb = cfg.get("max_file_mb")
        if max_mb is not None and not (0 < max_mb <= caps.max_file_mb):
            problems.append(f"{key}: max_file_mb {max_mb} must be above 0 and at most {caps.max_file_mb}")
        limits = cfg.get("limits") or {}
        for name in sorted(set(limits) - limit_names):
            problems.append(f"{key}: unknown limit '{name}'")
        unknown = set(cfg) - {"allowed", "subjects", "max_file_mb", "webhook", "limits"}
        for name in sorted(unknown):
            problems.append(f"{key}: unknown setting '{name}'")
        projects[key] = ProjectConfig(
            key, allowed, subjects, max_mb, cfg.get("webhook"),
            ProjectLimits(**{k: v for k, v in limits.items() if k in limit_names}),
        )  # fmt: skip
    if problems:
        raise ConfigError(problems)
    return projects
