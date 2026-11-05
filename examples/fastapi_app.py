"""The service endpoints (spec 9.3).

Run: uvicorn --factory examples.fastapi_app:create_app_from_env
"""

import asyncio
import hashlib
import json
import logging
import multiprocessing
import os
from concurrent.futures import TimeoutError as FutureTimeout

import pebble
from fastapi import FastAPI, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError

from pipeline_router import (
    FileFacts,
    FileRefused,
    NoPipeline,
    UnknownSubject,
    inspect_job,
    limit_child_memory,
)
from quiz_runner.export import to_csv, to_gift

from . import db, quiz_options
from .body_limit import BodySizeLimit
from .ratelimit import RateLimited
from .storage import upload_key

log = logging.getLogger("quiz_service")
MB = 2**20

STATUS = {
    "unknown_project": 401, "bad_request": 400, "empty_file": 400, "too_large": 413,
    "rate_limited": 429, "idempotency_mismatch": 422, "busy": 503, "too_complex": 422,
    "unsupported_type": 415, "damaged_file": 400, "encrypted": 400, "macros": 400,
    "zip_bomb": 400, "too_many_pages": 400, "pdf_javascript": 400, "image_too_large": 400,
    "poor_quality": 400,
    "unknown_subject": 400, "no_reader": 422, "not_allowed": 403, "no_match": 422,
    "upload_expired": 410, "invalid_options": 422, "not_found": 404, "not_ready": 409,
}  # fmt: skip
TOO_COMPLEX = "This file is too complex to check quickly. Try a smaller file or export it again as PDF."


class ApiError(Exception):
    def __init__(self, code: str, message: str, headers: dict | None = None):
        self.code, self.message, self.headers = code, message, headers or {}


def _error(code, message, headers=None):
    return JSONResponse({"error": code, "message": message}, STATUS.get(code, 400), headers=headers)


class QuizIn(BaseModel):
    count: int
    difficulty: str | dict = "medium"
    types: list[str] = Field(default_factory=list)
    focus: str | None = None  # only questions about this part of the material


class JobIn(BaseModel):
    upload_id: str
    quiz: QuizIn
    language: str | None = None
    subject: str | None = None
    client_ref: str | None = None


def create_app(ctx, probe_concurrency: int = 4, slot_wait_s: float = 2.0, pool=None, inspect=inspect_job):
    """`pool` and `inspect` are replaceable for tests."""
    caps = ctx.caps
    if pool is None:
        pool = pebble.ProcessPool(
            max_workers=probe_concurrency,
            max_tasks=200,  # recycle each child after 200 files
            initializer=limit_child_memory,
            initargs=(caps.probe_memory_mb,),
            context=multiprocessing.get_context("spawn"),  # no fork of a threaded process
        )
    slots = asyncio.Semaphore(probe_concurrency)
    api = FastAPI(title="AI quiz service")
    api.state.pool = pool

    @api.exception_handler(ApiError)
    async def api_error(request, e: ApiError):
        return _error(e.code, e.message, e.headers)

    def project_of(api_key: str | None):
        name = db.project_for_key(ctx.engine, api_key)
        if name is None or name not in ctx.projects:
            raise ApiError("unknown_project", "Missing or wrong API key.")
        return ctx.projects[name]

    def limit(project, kind, **kw):
        try:
            ctx.limiter.check(project, kind, **kw)
        except RateLimited as e:
            raise ApiError("rate_limited", e.message, {"Retry-After": str(e.retry_after)}) from None

    def refusal(e):
        if isinstance(e, (FileRefused, UnknownSubject, NoPipeline, quiz_options.BadOptions)):
            return ApiError(e.code, e.message)
        raise e

    async def inspect_file(data: bytes, project, filename):
        try:
            await asyncio.wait_for(slots.acquire(), slot_wait_s)
        except TimeoutError:
            raise ApiError("busy", "The service is busy. Try again in a few seconds.", {"Retry-After": "5"})
        try:
            future = pool.schedule(
                inspect, args=(data, project.max_file_mb, filename), timeout=caps.probe_timeout_s
            )
            return await asyncio.wrap_future(future)
        except FileRefused as e:
            raise ApiError(e.code, e.message) from None
        except (TimeoutError, FutureTimeout):
            raise ApiError("too_complex", TOO_COMPLEX) from None  # pebble already killed the child
        except Exception:
            log.exception("inspection child failed")
            raise ApiError("too_complex", TOO_COMPLEX) from None
        finally:
            slots.release()

    @api.get("/health")
    async def health():
        return {"ok": True}

    @api.post("/v1/route")
    async def route(
        file: UploadFile | None = File(None),
        text: str | None = Form(None),
        topic: str | None = Form(None),
        subject: str | None = Form(None),
        language: str | None = Form(None),
        x_api_key: str | None = Header(None),
    ):
        project = await asyncio.to_thread(project_of, x_api_key)
        given = [name for name, v in (("file", file), ("text", text), ("topic", topic)) if v is not None]
        if len(given) != 1:
            raise ApiError("bad_request", "Send exactly one of file, text or topic.")
        input_ = given[0]
        await asyncio.to_thread(limit, project, "route")
        digest = duplicate_of = None
        try:
            if input_ == "file":
                data = await file.read()  # already bounded by BodySizeLimit
                digest = hashlib.sha256(data).hexdigest()
                previous = await asyncio.to_thread(same_file, project.key, digest)
                if previous is not None:  # the same file again: its facts are known, nothing to inspect
                    facts, duplicate_of = FileFacts.from_dict(previous.facts), previous.id
                else:
                    facts = await inspect_file(data, project, file.filename)
                    await asyncio.to_thread(ctx.limiter.add_pages, project, facts.pages)
            elif input_ == "text":
                data = text.encode("utf-8")
                facts = ctx.routing.prober.text_facts(text)
            else:
                data = topic.encode("utf-8")
                facts = ctx.routing.prober.topic_facts(topic, language)
            decision = await asyncio.to_thread(ctx.routing.route, facts, input_, project, subject)
        except ApiError:
            raise
        except Exception as e:
            raise refusal(e) from None
        upload_id = db.new_id()

        def save():
            now = db.utcnow()
            ctx.storage.put(upload_key(upload_id), data)
            with ctx.engine.begin() as conn:
                conn.execute(
                    insert(db.uploads).values(
                        id=upload_id, project=project.key, input=input_,
                        filename=file.filename if file else None, sha256=digest, form_subject=subject,
                        facts=facts.to_dict(), decision=decision.to_dict(),
                        created_at=now, expires_at=now + db.UPLOAD_TTL,
                    )  # fmt: skip
                )

        await asyncio.to_thread(save)
        estimate = ctx.routing.estimate(facts)
        return {
            "upload_id": upload_id,
            "decision": decision.to_dict(),
            "estimate": estimate.__dict__,
            "duplicate_of": duplicate_of,
        }

    def same_file(project_key: str, digest: str):
        """This project's unexpired upload of exactly these bytes, if any."""
        with ctx.engine.connect() as conn:
            return conn.execute(
                select(db.uploads.c.id, db.uploads.c.facts)
                .where(
                    db.uploads.c.project == project_key,
                    db.uploads.c.sha256 == digest,
                    db.uploads.c.input == "file",
                    db.uploads.c.expires_at > db.utcnow(),
                )
                .order_by(db.uploads.c.created_at.desc())
                .limit(1)
            ).first()

    @api.post("/v1/jobs")  # plain def: FastAPI runs it in a thread, so DB calls don't block the loop
    def create_job(
        body: JobIn, x_api_key: str | None = Header(None), idempotency_key: str | None = Header(None)
    ):
        project = project_of(x_api_key)
        with ctx.engine.connect() as conn:
            active = conn.execute(
                select(func.count()).where(
                    db.jobs.c.project == project.key, db.jobs.c.status.in_(("queued", "running"))
                )
            ).scalar()
        limit(project, "job", active_jobs=active)
        key = idempotency_key or body.upload_id
        corrections = {"quiz": body.quiz.model_dump(), "language": body.language, "subject": body.subject}
        request_hash = hashlib.sha256(
            json.dumps(corrections, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        def existing():
            with ctx.engine.connect() as conn:
                row = conn.execute(
                    select(db.jobs).where(
                        db.jobs.c.project == project.key,
                        db.jobs.c.upload_id == body.upload_id,
                        db.jobs.c.idempotency_key == key,
                    )
                ).first()
            if row is None:
                return None
            if row.request_hash != request_hash:
                raise ApiError(
                    "idempotency_mismatch",
                    "This Idempotency-Key was already used with different options. "
                    "Use a new key for a new quiz.",
                )
            return JSONResponse({"job_id": row.id, "pipeline": row.pipeline, "status": "queued"}, 200)

        with ctx.engine.connect() as conn:
            upload = conn.execute(select(db.uploads).where(db.uploads.c.id == body.upload_id)).first()
        if upload is None or upload.project != project.key:
            raise ApiError("upload_expired", "This upload has expired. Upload the file again.")
        if (replay := existing()) is not None:
            return replay
        if upload.expires_at < db.utcnow():
            raise ApiError("upload_expired", "This upload has expired. Upload the file again.")

        decision = upload.decision
        try:
            if body.language or body.subject:  # corrections re-route the stored facts; nothing is inspected
                decision = ctx.routing.route(
                    FileFacts.from_dict(upload.facts), upload.input, project, upload.form_subject,
                    force_language=body.language, force_subject=body.subject,
                ).to_dict()  # fmt: skip
            options = quiz_options.validate(
                body.quiz.model_dump(), ctx.routing.router.get(decision["pipeline"]).config
            )
        except ApiError:
            raise
        except Exception as e:
            raise refusal(e) from None

        job_id = db.new_id()
        try:
            with ctx.engine.begin() as conn:
                conn.execute(
                    insert(db.jobs).values(
                        id=job_id, project=project.key, upload_id=upload.id, idempotency_key=key,
                        request_hash=request_hash, pipeline=decision["pipeline"], decision=decision,
                        quiz_options=options, client_ref=body.client_ref, status="queued",
                        attempts=0, requeue_count=0, stuck_alerted=False, enqueued_at=db.utcnow(),
                    )  # fmt: skip
                )
        except IntegrityError:
            replay = existing()  # a concurrent request with the same key won
            if replay is None:
                raise
            return replay
        try:
            ctx.enqueue_job(job_id)  # only after the row is committed
        except Exception:
            log.exception("queueing job %s failed", job_id)
            with ctx.engine.begin() as conn:
                conn.execute(delete(db.jobs).where(db.jobs.c.id == job_id))
            raise ApiError(
                "busy", "Couldn't queue the job. Try again with the same key.", {"Retry-After": "5"}
            )
        return JSONResponse({"job_id": job_id, "pipeline": decision["pipeline"], "status": "queued"}, 202)

    @api.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, x_api_key: str | None = Header(None)):
        project = project_of(x_api_key)
        with ctx.engine.connect() as conn:
            row = conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).first()
        if row is None or row.project != project.key:
            raise ApiError("not_found", "No such job.")
        return db.job_body(row)

    @api.get("/v1/jobs/{job_id}/export")
    def export_job(job_id: str, format: str = "json", x_api_key: str | None = Header(None)):
        project = project_of(x_api_key)
        with ctx.engine.connect() as conn:
            row = conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).first()
        if row is None or row.project != project.key:
            raise ApiError("not_found", "No such job.")
        if row.status != "succeeded":
            raise ApiError("not_ready", f"This quiz isn't ready to export (status: {row.status}).")
        if format == "json":
            return db.job_body(row)
        questions = row.result["questions"]
        if format == "csv":
            body, media, ext = to_csv(questions), "text/csv", "csv"
        elif format == "gift":
            body, media, ext = to_gift(questions), "text/plain", "gift.txt"
        else:
            raise ApiError("bad_request", "format must be json, csv or gift.")
        disposition = f'attachment; filename="quiz-{job_id}.{ext}"'
        return Response(
            body, media_type=f"{media}; charset=utf-8", headers={"Content-Disposition": disposition}
        )

    return BodySizeLimit(api, max_bytes=int(caps.max_file_mb * MB) + MB)


def create_app_from_env():
    from .context import from_env

    return create_app(from_env(), probe_concurrency=int(os.environ.get("PROBE_CONCURRENCY", 4)))
