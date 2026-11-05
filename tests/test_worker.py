import json
import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from fixtures import text_pdf
from sqlalchemy import event, insert, select, update
from svc import make_ctx

from examples import db, webhooks, worker
from examples.storage import upload_key


@pytest.fixture
def ctx(tmp_path):
    return make_ctx(tmp_path)


def add_job(ctx, pipeline="tamil_biology", options=None, status="queued", **cols):
    """An upload plus a committed job row, as POST /v1/jobs leaves them."""
    upload_id, job_id, now = db.new_id(), db.new_id(), db.utcnow()
    ctx.storage.put(upload_key(upload_id), text_pdf(2))
    with ctx.engine.begin() as conn:
        conn.execute(
            insert(db.uploads).values(
                id=upload_id, project="quicktest", input="file", facts={}, decision={},
                created_at=now, expires_at=now + db.UPLOAD_TTL,
            )  # fmt: skip
        )
        values = dict(
            id=job_id, project="quicktest", upload_id=upload_id, idempotency_key=upload_id,
            request_hash="h", pipeline=pipeline, decision={"pipeline": pipeline},
            quiz_options=options or {"count": 7, "types": ["mcq_single"], "difficulty": {"easy": 1.0}},
            status=status, attempts=0, requeue_count=0, stuck_alerted=False, enqueued_at=now,
        )  # fmt: skip
        conn.execute(insert(db.jobs).values({**values, **cols}))
    return job_id


def job(ctx, job_id):
    with ctx.engine.connect() as conn:
        return conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).one()


def set_job(ctx, job_id, **values):
    with ctx.engine.begin() as conn:
        conn.execute(update(db.jobs).where(db.jobs.c.id == job_id).values(**values))


def ago(**kw):
    return db.utcnow() - timedelta(**kw)


# ---- the job -------------------------------------------------------------------


def test_job_runs_with_the_job_layer_and_no_reroute(tmp_path):
    seen = []
    ctx = make_ctx(
        tmp_path, runner=lambda config, j, data: seen.append((config, j, data)) or {"questions": [1]}
    )
    job_id = add_job(ctx, "tamil_biology")
    assert worker.run_job(ctx, job_id) == "succeeded"
    config, j, data = seen[0]
    assert config["quiz"]["count"] == 7 and config["quiz"]["types"] == ["mcq_single"]
    assert config["readers"]["pdf"] == {"impl": "pymupdf"}  # the pipeline chosen at creation
    assert data.startswith(b"%PDF")
    row = job(ctx, job_id)
    assert (row.status, row.result, row.attempts) == ("succeeded", {"questions": [1]}, 1)


def test_redelivered_task_exits_without_work(tmp_path):
    calls = []
    ctx = make_ctx(tmp_path, runner=lambda *a: calls.append(1) or {})
    job_id = add_job(ctx)
    assert worker.run_job(ctx, job_id) == "succeeded"
    assert worker.run_job(ctx, job_id) == "skipped"
    assert len(calls) == 1


def test_stale_running_job_is_reclaimed(ctx):
    job_id = add_job(ctx, status="running", attempts=1, heartbeat_at=ago(minutes=6))
    assert worker.run_job(ctx, job_id) == "succeeded"
    assert job(ctx, job_id).attempts == 2


def test_fresh_running_job_is_not_taken(ctx):
    job_id = add_job(ctx, status="running", attempts=1, heartbeat_at=ago(minutes=1))
    assert worker.run_job(ctx, job_id) == "skipped"


def test_failure_is_recorded_with_code_and_webhook(tmp_path):
    class Refused(Exception):
        code, message = "no_text", "This file has no readable text."

    def runner(*a):
        raise Refused

    ctx = make_ctx(tmp_path, runner=runner)
    job_id = add_job(ctx)
    assert worker.run_job(ctx, job_id) == "failed"
    row = job(ctx, job_id)
    assert (row.error, row.message) == ("no_text", "This file has no readable text.")
    assert len(ctx.queued_webhooks) == 1


def test_slow_step_keeps_the_heartbeat_fresh(tmp_path):
    observed = {}

    def slow_runner(config, j, data):
        time.sleep(0.6)  # stands in for a 7-minute Gemini call; beats every 0.1 s here
        observed["heartbeat_at"] = job(ctx, j["id"]).heartbeat_at
        observed["sweep"] = worker.sweep(ctx)
        return {}

    ctx = make_ctx(tmp_path, runner=slow_runner)
    job_id = add_job(ctx)
    worker.run_job(ctx, job_id, heartbeat_s=0.1)
    assert observed["heartbeat_at"] > job(ctx, job_id).started_at
    assert observed["sweep"]["reclaimed"] == [] and job(ctx, job_id).attempts == 1


def test_heartbeat_uses_its_own_connection_and_closes_it(tmp_path, monkeypatch):
    by_thread = {}

    ctx = make_ctx(tmp_path, runner=lambda *a: time.sleep(0.3) or {})

    @event.listens_for(ctx.engine, "checkout")
    def checkout(dbapi_conn, record, proxy):
        hb_alive = any(t.name.startswith("heartbeat-") for t in threading.enumerate())
        by_thread.setdefault((threading.current_thread().name, hb_alive), set()).add(id(dbapi_conn))

    beats = []
    original = worker.Heartbeat

    class Spy(original):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            beats.append(self)

    monkeypatch.setattr(worker, "Heartbeat", Spy)
    job_id = add_job(ctx)
    worker.run_job(ctx, job_id, heartbeat_s=0.05)
    hb = beats[0]
    assert hb.beats >= 1 and not hb.is_alive()
    assert hb.connection.closed  # released when the thread stopped
    # while the heartbeat held its connection, the task never used that one
    (hb_conn,) = by_thread[(hb.name, True)]
    assert hb_conn not in by_thread.get((threading.current_thread().name, True), set())
    assert by_thread.get((threading.current_thread().name, True))  # the task did use the DB meanwhile


def test_heartbeat_after_finish_changes_nothing(ctx):
    job_id = add_job(ctx)
    worker.run_job(ctx, job_id)
    with ctx.engine.begin() as conn:
        db.beat(conn, job_id)
    assert job(ctx, job_id).status == "succeeded"
    assert not any(t.name.startswith("heartbeat-") for t in threading.enumerate())


def test_celery_settings():
    conf = worker.celery_app.conf
    assert conf.broker_transport_options["visibility_timeout"] == 3600
    task = worker.generate_quiz
    assert (task.acks_late, task.soft_time_limit, task.time_limit) == (True, 900, 960)
    assert conf.beat_schedule["sweep"]["schedule"] == 60.0


# ---- sweeper ---------------------------------------------------------------------


def test_job_never_queued_is_queued_after_two_minutes_and_runs_once(tmp_path):
    calls = []
    ctx = make_ctx(tmp_path, runner=lambda *a: calls.append(1) or {})
    job_id = add_job(ctx)  # API crashed between commit and delay()
    assert worker.sweep(ctx, now=db.utcnow() + timedelta(minutes=1))["requeued"] == []
    assert worker.sweep(ctx, now=db.utcnow() + timedelta(minutes=2, seconds=5))["requeued"] == [job_id]
    for queued in ctx.queued_jobs + [job_id]:  # the lost message turns up as well
        worker.run_job(ctx, queued)
    assert len(calls) == 1


def test_backlog_requeues_at_2_6_14_minutes_then_alerts(ctx):
    ids = [add_job(ctx) for _ in range(100)]
    start = db.utcnow()
    requeue_minutes = {}
    alerts = []
    for minute in range(1, 41):
        result = worker.sweep(ctx, now=start + timedelta(minutes=minute, seconds=1))
        for job_id in result["requeued"]:
            requeue_minutes.setdefault(job_id, []).append(minute)
        alerts += result["alerted"]
    assert all(requeue_minutes[i] == [2, 6, 14] for i in ids)
    assert sorted(alerts) == sorted(ids)  # one alert each
    assert all(job(ctx, i).status == "queued" for i in ids)


def test_killed_worker_is_requeued_and_attempts_is_two(ctx):
    job_id = add_job(ctx)
    with ctx.engine.begin() as conn:
        db.claim_job(conn, job_id)  # the worker claimed it and then died
    set_job(ctx, job_id, heartbeat_at=ago(minutes=6), enqueued_at=ago(minutes=6))
    assert worker.sweep(ctx)["reclaimed"] == [job_id]
    assert worker.sweep(ctx)["reclaimed"] == []  # not flooded every minute
    assert worker.run_job(ctx, job_id) == "succeeded"
    assert job(ctx, job_id).attempts == 2


def test_job_that_always_crashes_runs_three_times_then_fails(ctx):
    job_id = add_job(ctx)
    claims = 0
    for _ in range(5):
        with ctx.engine.begin() as conn:
            claims += db.claim_job(conn, job_id)  # the worker dies after each claim
        set_job(ctx, job_id, heartbeat_at=ago(minutes=6), enqueued_at=ago(minutes=6))
        worker.sweep(ctx)
    row = job(ctx, job_id)
    assert claims == 3 and (row.status, row.error) == ("failed", "worker_crashed")
    assert len(ctx.queued_webhooks) == 1


def test_expired_upload_without_job_is_deleted(ctx):
    upload_id = db.new_id()
    ctx.storage.put(upload_key(upload_id), b"data")
    with ctx.engine.begin() as conn:
        conn.execute(
            insert(db.uploads).values(
                id=upload_id, project="quicktest", input="file", facts={}, decision={},
                created_at=ago(minutes=31), expires_at=ago(minutes=1),
            )  # fmt: skip
        )
    kept_job = add_job(ctx)
    set_job(ctx, kept_job, status="succeeded")
    with ctx.engine.begin() as conn:
        conn.execute(update(db.uploads).where(db.uploads.c.id != upload_id).values(expires_at=ago(minutes=1)))
    assert worker.sweep(ctx)["expired_uploads"] == [upload_id]
    with pytest.raises(FileNotFoundError):
        ctx.storage.get(upload_key(upload_id))
    with ctx.engine.connect() as conn:
        assert conn.execute(select(db.uploads.c.id)).scalars().all() == [job(ctx, kept_job).upload_id]


# ---- webhooks -------------------------------------------------------------------


def test_verify_rules():
    secret, body, seen = "s3cret", b'{"job_id":"1"}', set()
    h = webhooks.sign_headers([secret], "ev-1", body, now=1000)
    assert webhooks.verify(secret, body, h, seen, now=1010) == "accepted"
    assert webhooks.verify(secret, body[:-1] + b"]", h, set(), now=1010) == "rejected"
    assert webhooks.verify(secret, body, h, set(), now=1000 + 6 * 60) == "rejected"
    assert webhooks.verify(secret, body, h, seen, now=1010) == "duplicate"


def test_rotation_signs_with_both_secrets(ctx):
    old = webhooks.rotate_secret(ctx.engine, ctx.fernet, "quicktest")
    new = webhooks.rotate_secret(ctx.engine, ctx.fernet, "quicktest")
    with ctx.engine.connect() as conn:
        active = webhooks.active_secrets(conn, ctx.fernet, "quicktest")
    assert active == [new, old]
    h = webhooks.sign_headers(active, "e", b"{}", now=5)
    assert (
        webhooks.verify(old, b"{}", h, set(), now=5)
        == webhooks.verify(new, b"{}", h, set(), now=5)
        == "accepted"
    )
    with ctx.engine.begin() as conn:
        conn.execute(
            update(db.webhook_secrets)
            .where(db.webhook_secrets.c.retire_at.is_not(None))
            .values(retire_at=ago(seconds=1))
        )
        assert webhooks.active_secrets(conn, ctx.fernet, "quicktest") == [new]


class FakeClient:
    def __init__(self, statuses):
        self.statuses, self.calls = list(statuses), []

    def __call__(self, url, content, headers, timeout):
        self.calls.append((url, content, dict(headers)))
        status = self.statuses.pop(0)
        if isinstance(status, Exception):
            raise status
        return type("R", (), {"status_code": status})()


def test_retry_hours_later_is_signed_again(ctx):
    secret = webhooks.rotate_secret(ctx.engine, ctx.fernet, "quicktest")
    job_id = add_job(ctx)
    worker.run_job(ctx, job_id)
    event_id, countdown = ctx.queued_webhooks[0]
    client = FakeClient([ConnectionError("down"), 200])
    t0 = time.time()
    assert worker.deliver_webhook(ctx, event_id, post=client, now=t0) == "pending"
    assert ctx.queued_webhooks[-1] == (event_id, 60)
    later = t0 + 2 * 3600
    assert worker.deliver_webhook(ctx, event_id, post=client, now=later) == "delivered"
    (_, body1, h1), (_, body2, h2) = client.calls
    assert body1 == body2 and h1["X-Quiz-Event-Id"] == h2["X-Quiz-Event-Id"] == event_id
    assert h2["X-Quiz-Timestamp"] != h1["X-Quiz-Timestamp"]
    assert webhooks.verify(secret, body2, h2, set(), now=later + 5) == "accepted"
    assert json.loads(body2)["status"] == "succeeded"
    assert worker.deliver_webhook(ctx, event_id, post=client) == "delivered"  # no resend


def test_gives_up_after_the_schedule(ctx):
    webhooks.rotate_secret(ctx.engine, ctx.fernet, "quicktest")
    job_id = add_job(ctx)
    worker.run_job(ctx, job_id)
    event_id = ctx.queued_webhooks[0][0]
    client = FakeClient([500] * 6)
    results = [worker.deliver_webhook(ctx, event_id, post=client) for _ in range(6)]
    assert results == ["pending"] * 5 + ["gave_up"]
    assert [c for _, c in ctx.queued_webhooks[1:]] == [60, 300, 1800, 7200, 43200]


def test_webhook_body_matches_get(tmp_path):
    from examples.fastapi_app import create_app
    from svc import ThreadPool

    ctx = make_ctx(tmp_path)
    webhooks.rotate_secret(ctx.engine, ctx.fernet, "quicktest")
    job_id = add_job(ctx)
    worker.run_job(ctx, job_id)
    client = TestClient(create_app(ctx, pool=ThreadPool()))
    got = client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": ctx.key}).json()
    fake = FakeClient([200])
    worker.deliver_webhook(ctx, ctx.queued_webhooks[0][0], post=fake)
    assert json.loads(fake.calls[0][1]) == got
