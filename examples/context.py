"""Everything the API and the worker share, built once per process from
environment variables (spec 9.1)."""

import importlib
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

from pipeline_router import (
    KeywordClassifier,
    PipelineRouter,
    Prober,
    RoutingService,
    SafetyCaps,
    SubjectResolver,
    TesseractScriptDetector,
    load_projects,
    pdf_crypto_self_test,
)

from quiz_runner import default_runner

from . import db
from .bank import QuestionBank
from .ratelimit import GLOBAL_JOBS_PER_DAY, RateLimiter
from .storage import LocalStorage, S3Storage

log = logging.getLogger("quiz_service")
HERE = Path(__file__).parent


@dataclass
class Context:
    routing: RoutingService
    projects: dict
    engine: object
    storage: object
    limiter: RateLimiter
    fernet: Fernet
    enqueue_job: Callable[[str], None]  # queue generate_quiz(job_id)
    enqueue_webhook: Callable[[str, int], None]  # queue send_webhook(event_id) after n seconds
    runner: Callable  # runner(config, job: dict, data: bytes) -> quiz dict
    caps: SafetyCaps


def _import(path: str):
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)


def load_dotenv(path=".env") -> None:
    """KEY=VALUE lines from .env into the environment; real environment
    variables win. Blank lines and # comments are skipped."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in ("'", '"') and value[1:].find(value[0]) >= 0:
            value = value[1 : 1 + value[1:].find(value[0])]  # quoted: taken as it is
        else:
            value = "" if value.startswith("#") else re.split(r"\s+#", value, maxsplit=1)[0].strip()
        os.environ.setdefault(key.strip(), value)


def from_env(enqueue_job=None, enqueue_webhook=None) -> Context:
    import redis

    load_dotenv(os.environ.get("ENV_FILE", ".env"))
    env = os.environ
    pdf_crypto_self_test()  # a missing cryptography package fails here, not on every PDF
    caps = SafetyCaps()
    router = PipelineRouter.from_directory(env.get("PIPELINES_DIR", HERE / "pipelines"))
    for warning in router.warnings:
        log.warning("pipeline config: %s", warning)
    projects = load_projects(env.get("PROJECTS_FILE", HERE / "projects.yaml"), router, caps)
    routing = RoutingService(
        router,
        prober=Prober(caps, image_script_detector=TesseractScriptDetector()),
        subjects=SubjectResolver(classifier=KeywordClassifier()),
        caps=caps,
    )
    if env.get("S3_BUCKET"):
        storage = S3Storage(
            env["S3_BUCKET"],
            endpoint_url=env.get("S3_ENDPOINT_URL"),
            region_name=env.get("S3_REGION"),
        )
    else:
        storage = LocalStorage(env.get("STORAGE_DIR", "var/storage"))
    engine = db.make_engine(env.get("DATABASE_URL", "sqlite:///var/quiz_service.db"))
    limiter = RateLimiter(
        redis.Redis.from_url(env.get("REDIS_URL", "redis://localhost:6379/0")),
        projects,
        int(env.get("GLOBAL_JOBS_PER_DAY", GLOBAL_JOBS_PER_DAY)),
    )
    if enqueue_job is None or enqueue_webhook is None:
        from . import worker

        enqueue_job = enqueue_job or (lambda job_id: worker.generate_quiz.delay(job_id))
        enqueue_webhook = enqueue_webhook or (
            lambda event_id, countdown=0: worker.send_webhook.apply_async((event_id,), countdown=countdown)
        )
    return Context(
        routing=routing,
        projects=projects,
        engine=engine,
        storage=storage,
        limiter=limiter,
        fernet=Fernet(env["WEBHOOK_KEY"].encode()),  # required: encrypts webhook secrets
        enqueue_job=enqueue_job,
        enqueue_webhook=enqueue_webhook,
        runner=(
            _import(env["QUIZ_RUNNER"]) if env.get("QUIZ_RUNNER") else default_runner(QuestionBank(engine))
        ),
        caps=caps,
    )
