"""A real service context for tests: SQLite file, fakeredis, local storage."""

import concurrent.futures as cf
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import fakeredis
from sqlalchemy import text
from cryptography.fernet import Fernet

from examples import db
from examples.context import Context
from examples.ratelimit import RateLimiter
from examples.storage import LocalStorage
from pipeline_router import (
    KeywordClassifier,
    PipelineRouter,
    Prober,
    ProjectConfig,
    ProjectLimits,
    RoutingService,
    SafetyCaps,
    SubjectResolver,
)

ROOT = Path(__file__).resolve().parent.parent
ENGINES = []  # disposed after each test (conftest), so Postgres connections don't pile up


def make_test_engine(tmp_path):
    """SQLite by default; the real Postgres when TEST_DATABASE_URL is set (fresh schema per test)."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        engine = db.make_engine(url, create=False)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        db.metadata.drop_all(engine)
        db.metadata.create_all(engine)
    else:
        engine = db.make_engine(f"sqlite:///{tmp_path}/service.db")
    ENGINES.append(engine)
    return engine


def make_redis():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        return fakeredis.FakeRedis()
    import redis

    client = redis.Redis.from_url(url)
    client.flushdb()
    return client


SUBJECTS = ("physics", "chemistry", "biology", "mathematics")


def make_ctx(tmp_path, runner=None, caps=None, limits=None, webhook="https://client.example/hook"):
    caps = caps or SafetyCaps()
    router = PipelineRouter.from_directory(ROOT / "examples" / "pipelines")
    projects = {
        "quicktest": ProjectConfig("quicktest", None, SUBJECTS, 20, webhook, limits or ProjectLimits()),
        "other": ProjectConfig("other", None, SUBJECTS, None, None),
    }
    routing = RoutingService(router, Prober(caps), SubjectResolver(KeywordClassifier()), caps)
    engine = make_test_engine(tmp_path)
    queued_jobs, queued_webhooks = [], []
    ctx = Context(
        routing=routing,
        projects=projects,
        engine=engine,
        storage=LocalStorage(tmp_path / "storage"),
        limiter=RateLimiter(make_redis(), projects),
        fernet=Fernet(Fernet.generate_key()),
        enqueue_job=queued_jobs.append,
        enqueue_webhook=lambda event_id, countdown=0: queued_webhooks.append((event_id, countdown)),
        runner=runner or (lambda config, job, data: {"questions": [], "config_quiz": config["quiz"]}),
        caps=caps,
    )
    ctx.queued_jobs, ctx.queued_webhooks = queued_jobs, queued_webhooks
    ctx.key = db.add_api_key(engine, "quicktest")
    ctx.other_key = db.add_api_key(engine, "other")
    return ctx


class ThreadPool:
    """Stands in for pebble in API tests that don't need real processes:
    same schedule(fn, args, timeout) shape, and it tracks concurrency."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.pool = cf.ThreadPoolExecutor(16)
        self.lock = threading.Lock()
        self.running = self.peak = 0

    def schedule(self, fn, args=(), timeout=None):
        def run():
            with self.lock:
                self.running += 1
                self.peak = max(self.peak, self.running)
            try:
                time.sleep(self.delay)
                return fn(*args)
            finally:
                with self.lock:
                    self.running -= 1

        return self.pool.submit(run)


__all__ = ["make_ctx", "ThreadPool", "replace"]
