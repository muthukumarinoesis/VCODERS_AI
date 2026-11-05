"""Decides which quiz pipeline handles a request. Framework-independent."""

from .caps import SafetyCaps
from .filetype import ACCEPTED_KINDS, FileRefused, read_entry, sniff
from .isolation import inspect_job, limit_child_memory
from .probe import FileFacts, Prober, TesseractScriptDetector, pdf_crypto_self_test, sample_indices
from .quality import PageQuality, QualityRules, file_quality, garbled_share, image_quality
from .projects import ProjectConfig, ProjectLimits, load_projects
from .router import (
    INPUTS,
    MATCH_KEYS,
    ConfigError,
    NoPipeline,
    PipelineRouter,
    PipelineSpec,
    RouteInput,
    RouteResult,
    load_pipelines,
    validate,
)
from .scripts import LanguageGuess, count_scripts, dominant_language
from .service import Decision, Estimate, RoutingService
from .subject import KeywordClassifier, LLMClassifier, SubjectGuess, SubjectResolver, UnknownSubject
from .textdecode import DecodedText, decode_text

__all__ = [
    "ACCEPTED_KINDS", "INPUTS", "MATCH_KEYS", "ConfigError", "Decision", "DecodedText",
    "Estimate", "FileFacts", "FileRefused", "KeywordClassifier", "LLMClassifier",
    "LanguageGuess", "NoPipeline", "PipelineRouter", "PipelineSpec", "Prober", "ProjectConfig",
    "PageQuality", "ProjectLimits", "QualityRules", "file_quality", "garbled_share", "image_quality",
    "RouteInput", "RouteResult", "RoutingService", "SafetyCaps", "SubjectGuess",
    "SubjectResolver", "TesseractScriptDetector", "UnknownSubject", "count_scripts",
    "decode_text", "dominant_language", "inspect_job", "limit_child_memory", "load_pipelines",
    "load_projects", "pdf_crypto_self_test", "read_entry", "sample_indices", "sniff", "validate",
]  # fmt: skip
