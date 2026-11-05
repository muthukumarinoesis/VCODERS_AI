"""Service tables and the queries shared by the API and the worker.

SQLAlchemy Core so the same code runs on Postgres (production) and SQLite
(tests). All times are naive UTC.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
    and_,
    create_engine,
    insert,
    or_,
    select,
    update,
)

from quiz_runner.embeddings import EMBED_DIM

MAX_ATTEMPTS = 3
STALE_HEARTBEAT = timedelta(minutes=5)
UPLOAD_TTL = timedelta(minutes=30)

metadata = MetaData()


class Embedding(TypeDecorator):
    """pgvector on Postgres (searched in SQL with an HNSW index); JSON elsewhere."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(EMBED_DIM))
        return dialect.type_descriptor(JSON())

    def process_result_value(self, value, dialect):
        return None if value is None else [float(v) for v in value]


api_keys = Table(
    "api_keys",
    metadata,
    Column("key_hash", String(64), primary_key=True),
    Column("project", String(64), nullable=False),
    Column("created_at", DateTime, nullable=False),
)

uploads = Table(
    "uploads",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("project", String(64), nullable=False, index=True),
    Column("input", String(8), nullable=False),
    Column("filename", String(255)),
    Column("sha256", String(64)),  # file bytes; the same file again from the same project is not re-inspected
    Column("form_subject", String(64)),
    Column("facts", JSON, nullable=False),
    Column("decision", JSON, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("expires_at", DateTime, nullable=False, index=True),
)
Index("uploads_same_file", uploads.c.project, uploads.c.sha256)

jobs = Table(
    "jobs",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("project", String(64), nullable=False, index=True),
    Column("upload_id", String(32), nullable=False, index=True),
    Column("idempotency_key", String(255), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("pipeline", String(128), nullable=False),
    Column("decision", JSON, nullable=False),
    Column("quiz_options", JSON, nullable=False),
    Column("client_ref", String(255)),
    Column("status", String(16), nullable=False, index=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("requeue_count", Integer, nullable=False, default=0),
    Column("stuck_alerted", Boolean, nullable=False, default=False),
    Column("enqueued_at", DateTime, nullable=False),
    Column("started_at", DateTime),
    Column("heartbeat_at", DateTime),
    Column("finished_at", DateTime),
    Column("result", JSON),
    Column("error", String(64)),
    Column("message", Text),
    UniqueConstraint("project", "upload_id", "idempotency_key", name="jobs_idempotency"),
)

bank_questions = Table(
    "bank_questions",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("project", String(64), nullable=False),  # a project's questions are never shown to another
    Column("language", String(16)),
    Column("subject", String(64)),
    Column("type", String(32), nullable=False),
    Column("difficulty", String(16), nullable=False),
    Column("question", JSON, nullable=False),
    Column("embedding", Embedding, nullable=False),
    Column("job_id", String(32), nullable=False),
    Column("created_at", DateTime, nullable=False),
)
Index("bank_questions_scope", bank_questions.c.project, bank_questions.c.language, bank_questions.c.type)
Index(
    "bank_questions_embedding_hnsw",
    bank_questions.c.embedding,
    postgresql_using="hnsw",
    postgresql_with={"m": 16, "ef_construction": 64},
    postgresql_ops={"embedding": "vector_cosine_ops"},
).ddl_if(dialect="postgresql")

webhook_secrets = Table(
    "webhook_secrets",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project", String(64), nullable=False, index=True),
    Column("secret_encrypted", LargeBinary, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("retire_at", DateTime),  # None while current; set to now + 24 h on rotation
)

webhook_deliveries = Table(
    "webhook_deliveries",
    metadata,
    Column("event_id", String(36), primary_key=True),
    Column("job_id", String(32), nullable=False, index=True),
    Column("project", String(64), nullable=False),
    Column("url", Text, nullable=False),
    Column("body", LargeBinary, nullable=False),  # exactly the same bytes on every attempt
    Column("attempts", Integer, nullable=False, default=0),
    Column("status", String(16), nullable=False),  # "pending" | "delivered" | "gave_up"
    Column("last_error", Text),
)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


def make_engine(url: str, create: bool | None = None):
    """Postgres gets its schema from migrations (`alembic upgrade head`);
    SQLite (dev, tests) is created directly. `create` overrides that."""
    connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    if create if create is not None else engine.dialect.name == "sqlite":
        metadata.create_all(engine)
    return engine


# ---- API keys ---------------------------------------------------------------


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def project_for_key(engine, raw: str | None) -> str | None:
    if not raw:
        return None
    with engine.connect() as conn:
        return conn.execute(select(api_keys.c.project).where(api_keys.c.key_hash == hash_key(raw))).scalar()


def add_api_key(engine, project: str) -> str:
    """Returns the raw key once; only its hash is stored."""
    import secrets

    raw = "qk_" + secrets.token_urlsafe(32)
    with engine.begin() as conn:
        conn.execute(insert(api_keys).values(key_hash=hash_key(raw), project=project, created_at=utcnow()))
    return raw


# ---- jobs: claim and heartbeat -----------------------------------------------


def claim_job(conn, job_id: str, now: datetime | None = None) -> bool:
    """Atomic: a redelivered message never runs a job twice."""
    now = now or utcnow()
    result = conn.execute(
        update(jobs)
        .where(
            jobs.c.id == job_id,
            jobs.c.attempts < MAX_ATTEMPTS,
            or_(
                jobs.c.status == "queued",
                and_(jobs.c.status == "running", jobs.c.heartbeat_at < now - STALE_HEARTBEAT),
            ),
        )
        .values(status="running", started_at=now, heartbeat_at=now, attempts=jobs.c.attempts + 1)
    )
    return result.rowcount == 1


def beat(conn, job_id: str, now: datetime | None = None) -> None:
    """Only heartbeat_at, and only while running: can't revive a finished job."""
    conn.execute(
        update(jobs)
        .where(jobs.c.id == job_id, jobs.c.status == "running")
        .values(heartbeat_at=now or utcnow())
    )


def finish_job(conn, job_id: str, status: str, result=None, error=None, message=None) -> bool:
    out = conn.execute(
        update(jobs)
        .where(jobs.c.id == job_id, jobs.c.status == "running")
        .values(status=status, finished_at=utcnow(), result=result, error=error, message=message)
    )
    return out.rowcount == 1


def job_body(row) -> dict:
    """The body of GET /v1/jobs/{id}; webhooks send the same."""
    body = {"job_id": row.id, "status": row.status, "pipeline": row.pipeline, "client_ref": row.client_ref}
    if row.status == "succeeded":
        body["quiz"] = row.result
    elif row.status == "failed":
        body["error"], body["message"] = row.error, row.message
    return body
