import asyncio
import multiprocessing
import os
import threading
import time
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import httpx
import pebble
import pytest
from fastapi.testclient import TestClient
from fixtures import TAMIL, scanned_pdf, text_pdf
from sqlalchemy import func, select, update
from svc import ThreadPool, make_ctx

from examples import db
from examples.body_limit import BodySizeLimit
from examples.fastapi_app import create_app
from pipeline_router import ProjectLimits, SafetyCaps

MB = 2**20


def app_for(ctx, pool=None, **kw):
    pool = pool or ThreadPool()
    return create_app(ctx, pool=pool, inspect=lambda d, mb, hint: ctx.routing.inspect(d, mb, hint), **kw)


@pytest.fixture
def ctx(tmp_path):
    return make_ctx(tmp_path)


@pytest.fixture
def client(ctx):
    return TestClient(app_for(ctx))


def headers(ctx, **extra):
    return {"X-API-Key": ctx.key, **extra}


def route_pdf(client, ctx, data=None, **form):
    return client.post(
        "/v1/route", headers=headers(ctx), files={"file": ("a.pdf", data or text_pdf(3))}, data=form
    )


def new_upload(client, ctx):
    r = route_pdf(client, ctx, subject="biology")
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


QUIZ = {"count": 10, "difficulty": "easy"}


# ---- /v1/route ----------------------------------------------------------------


def test_route_a_file(client, ctx):
    r = route_pdf(client, ctx)
    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["pipeline"] == "default" and body["estimate"]["reading"] == "text"
    assert ctx.storage.get(f"uploads/{body['upload_id']}").startswith(b"%PDF")


def test_route_text_and_topic(client, ctx):
    r = client.post("/v1/route", headers=headers(ctx), data={"text": TAMIL * 3, "subject": "biology"})
    assert r.json()["decision"]["pipeline"] == "tamil_text"
    r = client.post("/v1/route", headers=headers(ctx), data={"topic": "Photosynthesis", "language": "ta"})
    assert r.json()["decision"]["pipeline"] == "topic_bank"


def test_route_errors(client, ctx):
    assert client.post("/v1/route", data={"text": "x"}).json()["error"] == "unknown_project"
    assert client.post("/v1/route", headers=headers(ctx), data={"text": "a", "topic": "b"}).status_code == 400
    r = route_pdf(client, ctx, data=b"\x00\x01\x02" * 100)
    assert (r.status_code, r.json()["error"]) == (415, "unsupported_type")
    r = route_pdf(client, ctx, subject="history")
    assert (r.status_code, r.json()["error"]) == (400, "unknown_subject")


def test_not_allowed_is_403(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.projects["quicktest"] = replace(ctx.projects["quicktest"], allowed=frozenset({"tamil_biology"}))
    r = route_pdf(TestClient(app_for(ctx)), ctx)
    assert (r.status_code, r.json()["error"]) == (403, "not_allowed")


def test_61st_route_call_is_rate_limited(client, ctx):
    for _ in range(60):
        assert client.post("/v1/route", headers=headers(ctx), data={"topic": "cells"}).status_code == 200
    r = client.post("/v1/route", headers=headers(ctx), data={"topic": "cells"})
    assert (r.status_code, r.json()["error"]) == (429, "rate_limited")
    assert int(r.headers["Retry-After"]) >= 1


def test_50_mb_multipart_passes_the_middleware_and_inspect_checks_size(client, ctx):
    r = route_pdf(client, ctx, data=b"%PDF-" + b"\x00" * (50 * MB - 5))
    assert r.status_code == 413
    message = r.json()["message"]
    assert "50.0 MB" in message and "20 MB" in message  # inspect()'s message, not the middleware's


# ---- body limit (raw ASGI) ------------------------------------------------------


async def _call(app, headers, chunks):
    pulled, sent = [], []

    async def receive():
        if len(pulled) >= len(chunks):
            raise AssertionError("body read past the end")
        pulled.append(chunks[len(pulled)])
        return {"type": "http.request", "body": pulled[-1], "more_body": len(pulled) < len(chunks)}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    await app(scope, receive, send)
    return pulled, sent


async def _reading_app(scope, receive, send):
    while (await receive()).get("more_body"):
        pass
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_content_length_over_the_cap_is_413_before_any_read():
    app = BodySizeLimit(_reading_app, 51 * MB)
    pulled, sent = asyncio.run(_call(app, [(b"content-length", str(60 * MB).encode())], [b"x"]))
    assert pulled == [] and sent[0]["status"] == 413


def test_chunked_upload_over_the_cap_stops_after_one_chunk_past():
    chunk = b"x" * MB
    app = BodySizeLimit(_reading_app, 5 * MB)
    pulled, sent = asyncio.run(_call(app, [], [chunk] * 20))
    assert sent[0]["status"] == 413 and len(sent) == 2
    assert sum(map(len, pulled)) <= 5 * MB + len(chunk)


def test_app_error_after_rejection_is_dropped():
    async def app(scope, receive, send):
        try:
            while (await receive()).get("more_body"):
                pass
        except Exception:
            await send({"type": "http.response.start", "status": 500, "headers": []})
            raise

    pulled, sent = asyncio.run(_call(BodySizeLimit(app, MB), [], [b"x" * MB] * 3))
    assert [m.get("status") for m in sent if m["type"] == "http.response.start"] == [413]


# ---- isolation with real pebble children ---------------------------------------


@pytest.fixture
def spawn_pool():
    pools = []

    def make(**kw):
        pool = pebble.ProcessPool(context=multiprocessing.get_context("spawn"), **kw)
        pools.append(pool)
        return pool

    yield make
    for pool in pools:
        pool.stop()
        pool.join()


def test_slow_inspection_is_killed_and_too_complex(tmp_path, spawn_pool):
    from child_fakes import slow_inspect

    ctx = make_ctx(tmp_path, caps=SafetyCaps(probe_timeout_s=2))
    app = create_app(ctx, pool=spawn_pool(max_workers=1), inspect=slow_inspect)
    pid_file = tmp_path / "pid"
    start = time.monotonic()
    r = TestClient(app).post("/v1/route", headers=headers(ctx), files={"file": ("a", str(pid_file).encode())})
    assert (r.status_code, r.json()["error"]) == (422, "too_complex")
    assert time.monotonic() - start < 15
    if not pid_file.exists():
        return  # killed before it even wrote its pid (a slow start under load): nothing left to check
    pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("the inspection child is still alive")


def test_inspection_past_the_memory_limit(tmp_path, spawn_pool):
    from child_fakes import hog_inspect

    from pipeline_router import limit_child_memory

    ctx = make_ctx(tmp_path)
    pool = spawn_pool(max_workers=1, initializer=limit_child_memory, initargs=(512,))
    client = TestClient(create_app(ctx, pool=pool, inspect=hog_inspect))
    r = client.post("/v1/route", headers=headers(ctx), files={"file": ("a", b"x")})
    assert (r.status_code, r.json()["error"]) == (422, "too_complex")
    assert client.get("/health").json() == {"ok": True}


def test_pebble_future_with_wrap_future(spawn_pool):
    from child_fakes import echo, sleepy

    pool = spawn_pool(max_workers=1)

    async def main():
        assert await asyncio.wrap_future(pool.schedule(echo, args=(41,))) == 41
        with pytest.raises(TimeoutError):
            await asyncio.wrap_future(pool.schedule(sleepy, args=(10,), timeout=1))

    asyncio.run(main())


# ---- slots ------------------------------------------------------------------------


def test_all_slots_held_returns_busy(ctx):
    app = app_for(ctx, pool=ThreadPool(delay=2.0), probe_concurrency=1, slot_wait_s=0.5)

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            files = {"file": ("a.pdf", text_pdf(2))}
            first = asyncio.create_task(c.post("/v1/route", headers=headers(ctx), files=files))
            await asyncio.sleep(0.2)
            start = time.monotonic()
            second = await c.post("/v1/route", headers=headers(ctx), files=files)
            waited = time.monotonic() - start
            await first
            return second, waited

    second, waited = asyncio.run(main())
    assert (second.status_code, second.json()["error"]) == (503, "busy")
    assert second.headers["Retry-After"] == "5" and waited < 1.5


def test_ten_concurrent_scans_respect_the_limit(ctx):
    pool = ThreadPool(delay=0.3)
    app = app_for(
        ctx, pool=pool, probe_concurrency=2, slot_wait_s=30
    )  # this test is about the limit, not speed
    scan = scanned_pdf(2)

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
            uploads = [
                asyncio.create_task(
                    c.post("/v1/route", headers=headers(ctx), files={"file": ("s.pdf", scan)})
                )
                for _ in range(10)
            ]
            await asyncio.sleep(0.1)
            start = time.monotonic()
            health = await c.get("/health")
            health_s = time.monotonic() - start
            return await asyncio.gather(*uploads), health, health_s

    results, health, health_s = asyncio.run(main())
    assert all(r.status_code == 200 for r in results)
    assert pool.peak <= 2
    assert health.status_code == 200 and health_s < 1.5  # answered while inspections run


# ---- /v1/jobs ------------------------------------------------------------------------


def job_rows(ctx):
    with ctx.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(db.jobs)).scalar()


def test_create_job_and_poll(client, ctx):
    upload = new_upload(client, ctx)
    r = client.post("/v1/jobs", headers=headers(ctx), json={"upload_id": upload, "quiz": QUIZ})
    assert r.status_code == 202
    job = r.json()
    assert job["status"] == "queued" and ctx.queued_jobs == [job["job_id"]]
    got = client.get(f"/v1/jobs/{job['job_id']}", headers=headers(ctx)).json()
    assert got["status"] == "queued"
    assert client.get(f"/v1/jobs/{job['job_id']}", headers={"X-API-Key": ctx.other_key}).status_code == 404


def test_same_upload_twice_without_key_is_one_job(client, ctx):
    upload = new_upload(client, ctx)
    a = client.post("/v1/jobs", headers=headers(ctx), json={"upload_id": upload, "quiz": QUIZ})
    b = client.post("/v1/jobs", headers=headers(ctx), json={"upload_id": upload, "quiz": QUIZ})
    assert (a.status_code, b.status_code) == (202, 200)
    assert a.json() == b.json() and job_rows(ctx) == 1 and len(ctx.queued_jobs) == 1


def test_concurrent_same_key_is_one_job(client, ctx):
    upload = new_upload(client, ctx)
    results = []

    def post():
        results.append(
            client.post(
                "/v1/jobs",
                headers=headers(ctx, **{"Idempotency-Key": "k1"}),
                json={"upload_id": upload, "quiz": QUIZ},
            )
        )

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({r.json()["job_id"] for r in results}) == 1 and job_rows(ctx) == 1


def test_lost_race_on_the_unique_constraint_returns_the_winner(client, ctx):
    upload = new_upload(client, ctx)
    real_new_id = db.new_id
    body = {"upload_id": upload, "quiz": QUIZ}
    winner = {}

    def racing_new_id():
        # Another request commits the same key just before this one inserts.
        with patch.object(db, "new_id", real_new_id):
            winner["r"] = client.post("/v1/jobs", headers=headers(ctx, **{"Idempotency-Key": "k"}), json=body)
        return real_new_id()

    with patch("examples.fastapi_app.db.new_id", side_effect=racing_new_id):
        r = client.post("/v1/jobs", headers=headers(ctx, **{"Idempotency-Key": "k"}), json=body)
    assert r.status_code == 200 and r.json()["job_id"] == winner["r"].json()["job_id"]
    assert job_rows(ctx) == 1


def test_different_keys_are_two_jobs(client, ctx):
    upload = new_upload(client, ctx)
    for key in ("a", "b"):
        client.post(
            "/v1/jobs",
            headers=headers(ctx, **{"Idempotency-Key": key}),
            json={"upload_id": upload, "quiz": QUIZ},
        )
    assert job_rows(ctx) == 2


def test_same_key_different_count_is_a_mismatch(client, ctx):
    upload = new_upload(client, ctx)
    h = headers(ctx, **{"Idempotency-Key": "k"})
    client.post("/v1/jobs", headers=h, json={"upload_id": upload, "quiz": QUIZ})
    r = client.post("/v1/jobs", headers=h, json={"upload_id": upload, "quiz": {**QUIZ, "count": 11}})
    assert (r.status_code, r.json()["error"]) == (422, "idempotency_mismatch") and job_rows(ctx) == 1


def test_corrections_reroute_without_inspecting(client, ctx):
    upload = new_upload(client, ctx)
    with patch.object(ctx.routing, "inspect", side_effect=AssertionError("inspected again")):
        r = client.post(
            "/v1/jobs", headers=headers(ctx), json={"upload_id": upload, "quiz": QUIZ, "language": "ta"}
        )
    assert r.json()["pipeline"] == "tamil_biology"
    with ctx.engine.connect() as conn:
        decision = conn.execute(select(db.jobs.c.decision)).scalar()
    assert decision["language_source"] == "user"


def test_bad_options_and_expired_upload(client, ctx):
    upload = new_upload(client, ctx)
    r = client.post("/v1/jobs", headers=headers(ctx), json={"upload_id": upload, "quiz": {"count": 0}})
    assert (r.status_code, r.json()["error"]) == (422, "invalid_options")
    r = client.post(
        "/v1/jobs",
        headers=headers(ctx),
        json={"upload_id": upload, "quiz": {"count": 5, "difficulty": {"easy": 0.5}}},
    )
    assert r.json()["error"] == "invalid_options"
    with ctx.engine.begin() as conn:
        conn.execute(update(db.uploads).values(expires_at=db.utcnow() - timedelta(minutes=1)))
    r = client.post("/v1/jobs", headers=headers(ctx), json={"upload_id": upload, "quiz": QUIZ})
    assert (r.status_code, r.json()["error"]) == (410, "upload_expired")


def test_eleventh_job_while_ten_running(tmp_path):
    ctx = make_ctx(tmp_path, limits=ProjectLimits(jobs_per_minute=100))
    client = TestClient(app_for(ctx))
    upload = new_upload(client, ctx)
    for i in range(10):
        h = headers(ctx, **{"Idempotency-Key": f"k{i}"})
        assert client.post("/v1/jobs", headers=h, json={"upload_id": upload, "quiz": QUIZ}).status_code == 202
    r = client.post(
        "/v1/jobs",
        headers=headers(ctx, **{"Idempotency-Key": "k10"}),
        json={"upload_id": upload, "quiz": QUIZ},
    )
    assert (r.status_code, r.json()["error"]) == (429, "rate_limited")


def test_queueing_failure_deletes_the_row(tmp_path):
    ctx = make_ctx(tmp_path)
    client = TestClient(app_for(ctx))
    upload = new_upload(client, ctx)

    def broken(job_id):
        raise ConnectionError("broker down")

    ctx.enqueue_job = broken
    r = client.post("/v1/jobs", headers=headers(ctx), json={"upload_id": upload, "quiz": QUIZ})
    assert (r.status_code, r.json()["error"]) == (503, "busy") and job_rows(ctx) == 0
