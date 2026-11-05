"""Reads an upload and makes the quiz. Framework-independent, like pipeline_router."""

from .errors import RunnerError
from .gemini import DEFAULT_MODEL, GeminiClient
from .questions import SUPPORTED_TYPES, KatexChecker, normalize
from .readers import Page, read
from .embeddings import EMBED_DIM, EMBED_MODEL, GeminiEmbedder
from .runner import QuizRunner, default_runner

__all__ = [
    "DEFAULT_MODEL",
    "EMBED_DIM",
    "EMBED_MODEL",
    "GeminiClient",
    "GeminiEmbedder",
    "KatexChecker",
    "Page",
    "QuizRunner",
    "RunnerError",
    "SUPPORTED_TYPES",
    "default_runner",
    "normalize",
    "read",
]
