"""Celery worker: generate_quiz, webhooks and the sweeper (spec 9.6).

Run:  celery -A examples.worker worker -Q quiz   and   celery -A examples.worker beat
"""

import json
import logging
import os
import threading
from datetime import timedelta

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import and_, delete, exists, insert, select, update

from . import db
from .quiz_options import apply_job_layer
from .storage import upload_key
from .webhooks import RETRY_DELAYS_S, active_secrets, new_event_id, sign_headers

log = logging.getLogger("quiz_service.worker")

celery_app = Celery("quiz_service", broker=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
celery_app.conf.update(
    # Longer than the longest job (16-minute hard limit), or Redis redelivers
    # running tasks: the QUIC-55 lesson.
    broker_transport_options={"visibility_timeout": 3600},
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="quiz",
    beat_schedule={"sweep": {"task": "examples.worker.sweep_task", "schedule": 60.0}},
)

FIRST_REQUEUE = timedelta(minutes=2)
MAX_REQUEUES = 3
HEARTBEAT_S = 60

_ctx = None


def context():
    global _ctx
    if _ctx is None:
        from .context import from_env

        _ctx = from_env(
            enqueue_job=lambda job_id: generate_quiz.delay(job_id),
            enqueue_webhook=lambda event_id, countdown=0: send_webhook.apply_async(
                (event_id,), countdown=countdown
            ),
        )
    return _ctx


# ---- the job ------------------------------------------------------------------


class Heartbeat(threading.Thread):
    """Keeps heartbeat_at fresh while a slow step runs. Opens its own DB
    connection: connections must never be shared between threads."""

    def __init__(self, engine, job_id: str, interval_s: float = HEARTBEAT_S):
        super().__init__(daemon=True, name=f"heartbeat-{job_id}")
        self.engine, self.job_id, self.interval_s = engine, job_id, interval_s
        self._stop_event = threading.Event()
        self.connection = None
        self.beats = 0

    def run(self):
        with self.engine.connect() as conn:  # closed when the thread stops
            self.connection = conn
            while not self._stop_event.wait(self.interval_s):
                try:
                    with conn.begin():
                        db.beat(conn, self.job_id)
                    self.beats += 1
                except Exception:
                    log.exception("heartbeat for job %s failed", self.job_id)

    def stop(self):
        self._stop_event.set()
        self.join(timeout=10)


def run_job(ctx, job_id: str, heartbeat_s: float = HEARTBEAT_S) -> str:
    with ctx.engine.begin() as conn:
        if not db.claim_job(conn, job_id):
            return "skipped"  # running elsewhere, finished, or out of attempts
    heartbeat = Heartbeat(ctx.engine, job_id, heartbeat_s)
    heartbeat.start()
    job = None
    try:
        with ctx.engine.connect() as conn:
            job = conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).one()._asdict()
        # Never re-route: the pipeline chosen at job creation runs, whatever the file turns out to be.
        config = apply_job_layer(ctx.routing.router.get(job["pipeline"]).config, job["quiz_options"])
        data = ctx.storage.get(upload_key(job["upload_id"]))
        outcome = dict(status="succeeded", result=ctx.runner(config, job, data))
    except SoftTimeLimitExceeded:
        outcome = dict(
            status="failed", error="timed_out", message="Making this quiz took too long. Try fewer questions."
        )
    except Exception as e:
        log.exception("job %s failed; decision %s", job_id, json.dumps(job and job["decision"]))
        outcome = dict(
            status="failed",
            error=getattr(e, "code", "pipeline_error"),
            message=getattr(e, "message", "The quiz couldn't be made. Try again later."),
        )
    finally:
        heartbeat.stop()
    with ctx.engine.begin() as conn:
        finished = db.finish_job(conn, job_id, **outcome)
    if finished:
        queue_webhook(ctx, job_id)
    return outcome["status"]


# ---- webhooks ------------------------------------------------------------------


def queue_webhook(ctx, job_id: str) -> str | None:
    with ctx.engine.begin() as conn:
        job = conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).one()
        project = ctx.projects.get(job.project)
        if project is None or not project.webhook:
            return None
        event_id = new_event_id()
        body = json.dumps(db.job_body(job), sort_keys=True, separators=(",", ":")).encode()
        conn.execute(
            insert(db.webhook_deliveries).values(
                event_id=event_id, job_id=job_id, project=job.project, url=project.webhook,
                body=body, attempts=0, status="pending",
            )  # fmt: skip
        )
    ctx.enqueue_webhook(event_id, 0)
    return event_id


def deliver_webhook(ctx, event_id: str, post=None, now: float | None = None) -> str:
    """One attempt. Signed again every time with a fresh timestamp; the event
    ID and body never change."""
    import httpx

    post = post or httpx.post
    with ctx.engine.connect() as conn:
        d = conn.execute(
            select(db.webhook_deliveries).where(db.webhook_deliveries.c.event_id == event_id)
        ).one()
        secrets = active_secrets(conn, ctx.fernet, d.project)
    if d.status != "pending":
        return d.status
    error = None
    if not secrets:
        error = "no webhook secret for this project"
    else:
        try:
            response = post(
                d.url, content=d.body, headers=sign_headers(secrets, event_id, d.body, now), timeout=10
            )
            if not 200 <= response.status_code < 300:
                error = f"HTTP {response.status_code}"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
    attempts = d.attempts + 1
    if error is None:
        status = "delivered"
    elif attempts <= len(RETRY_DELAYS_S):
        status = "pending"
    else:
        status = "gave_up"  # GET /v1/jobs/{id} stays the source of truth
    with ctx.engine.begin() as conn:
        conn.execute(
            update(db.webhook_deliveries)
            .where(db.webhook_deliveries.c.event_id == event_id)
            .values(attempts=attempts, status=status, last_error=error)
        )
    if status == "pending":
        ctx.enqueue_webhook(event_id, RETRY_DELAYS_S[attempts - 1])
    return status


# ---- sweeper (every minute; every step is safe to repeat) -----------------------


def sweep(ctx, now=None) -> dict:
    now = now or db.utcnow()
    jobs = db.jobs
    out = {"requeued": [], "reclaimed": [], "crashed": [], "alerted": [], "expired_uploads": []}
    with ctx.engine.connect() as conn:
        queued = conn.execute(
            select(jobs.c.id, jobs.c.requeue_count, jobs.c.enqueued_at, jobs.c.stuck_alerted).where(
                jobs.c.status == "queued", jobs.c.enqueued_at <= now - FIRST_REQUEUE
            )
        ).all()
        stale = conn.execute(
            select(jobs.c.id, jobs.c.attempts, jobs.c.enqueued_at).where(
                jobs.c.status == "running", jobs.c.heartbeat_at < now - db.STALE_HEARTBEAT
            )
        ).all()

    # 1. Lost tasks, with backoff: 2, then 4, then 8 minutes; at most 3 re-queues.
    for job in queued:
        if job.requeue_count >= MAX_REQUEUES:
            if not job.stuck_alerted and now - job.enqueued_at >= FIRST_REQUEUE * 2**MAX_REQUEUES:
                log.error(
                    "ALERT: job %s still queued after %d re-queues: broker problem or backlog",
                    job.id,
                    MAX_REQUEUES,
                )
                with ctx.engine.begin() as conn:
                    conn.execute(update(jobs).where(jobs.c.id == job.id).values(stuck_alerted=True))
                out["alerted"].append(job.id)
            continue
        if now - job.enqueued_at < FIRST_REQUEUE * 2**job.requeue_count:  # at least 2, 4, 8 minutes
            continue
        with ctx.engine.begin() as conn:
            moved = conn.execute(
                update(jobs)
                .where(
                    jobs.c.id == job.id, jobs.c.status == "queued", jobs.c.requeue_count == job.requeue_count
                )
                .values(enqueued_at=now, requeue_count=job.requeue_count + 1)
            ).rowcount
        if moved:
            ctx.enqueue_job(job.id)
            out["requeued"].append(job.id)

    # 2. Dead workers: stale heartbeat.
    for job in stale:
        if job.attempts < db.MAX_ATTEMPTS:
            if job.enqueued_at > now - db.STALE_HEARTBEAT:
                continue  # already re-queued recently; the claim will take it over
            with ctx.engine.begin() as conn:
                moved = conn.execute(
                    update(jobs)
                    .where(jobs.c.id == job.id, jobs.c.enqueued_at == job.enqueued_at)
                    .values(enqueued_at=now)
                ).rowcount
            if moved:
                ctx.enqueue_job(job.id)
                out["reclaimed"].append(job.id)
        else:
            with ctx.engine.begin() as conn:
                failed = conn.execute(
                    update(jobs)
                    .where(
                        jobs.c.id == job.id,
                        jobs.c.status == "running",
                        jobs.c.heartbeat_at < now - db.STALE_HEARTBEAT,
                    )
                    .values(
                        status="failed", finished_at=now, error="worker_crashed",
                        message="The quiz couldn't be made: this file stopped the worker every time.",
                    )  # fmt: skip
                ).rowcount
            if failed:
                queue_webhook(ctx, job.id)  # so the client refunds
                out["crashed"].append(job.id)

    # 3. Expired uploads with no job: storage first, then the row.
    with ctx.engine.connect() as conn:
        expired = conn.execute(
            select(db.uploads.c.id).where(
                db.uploads.c.expires_at < now, ~exists().where(jobs.c.upload_id == db.uploads.c.id)
            )
        ).all()
    for (upload_id,) in expired:
        ctx.storage.delete(upload_key(upload_id))
        with ctx.engine.begin() as conn:
            conn.execute(
                delete(db.uploads).where(
                    and_(db.uploads.c.id == upload_id, ~exists().where(jobs.c.upload_id == upload_id))
                )
            )
        out["expired_uploads"].append(upload_id)
    return out


# ---- Celery tasks: arguments are only IDs; everything else is read from the DB ---


@celery_app.task(
    bind=True, acks_late=True, soft_time_limit=900, time_limit=960, name="examples.worker.generate_quiz"
)
def generate_quiz(self, job_id: str):
    return run_job(context(), job_id)


@celery_app.task(acks_late=True, name="examples.worker.send_webhook")
def send_webhook(event_id: str):
    return deliver_webhook(context(), event_id)


@celery_app.task(name="examples.worker.sweep_task")
def sweep_task():
    result = sweep(context())
    return {k: len(v) for k, v in result.items()}
