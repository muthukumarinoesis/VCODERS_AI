"""Signed webhooks (spec 9.4).

Header: X-Quiz-Signature: v1=<hex HMAC-SHA256(secret, timestamp + "." + body)>.
During a rotation both active secrets sign, comma-separated, so a client
holding either one verifies.
"""

import hashlib
import hmac
import time
import uuid
from datetime import timedelta

from cryptography.fernet import Fernet
from sqlalchemy import insert, or_, select, update

from .db import utcnow, webhook_secrets

TOLERANCE_S = 300
RETRY_DELAYS_S = (60, 300, 1800, 7200, 43200)  # 1 min, 5 min, 30 min, 2 h, 12 h
ROTATION_OVERLAP = timedelta(hours=24)


def signature(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256)
    return "v1=" + mac.hexdigest()


def sign_headers(secrets: list[str], event_id: str, body: bytes, now: float | None = None) -> dict:
    timestamp = int(now if now is not None else time.time())
    return {
        "Content-Type": "application/json",
        "X-Quiz-Event-Id": event_id,
        "X-Quiz-Timestamp": str(timestamp),
        "X-Quiz-Signature": ",".join(signature(s, timestamp, body) for s in secrets),
    }


def verify(secret: str, body: bytes, headers, seen, now: float | None = None) -> str:
    """What a client does. Returns "accepted", "rejected" or "duplicate".
    `seen` is a set (or anything with `in` and `add`) of event IDs."""
    headers = {k.lower(): v for k, v in headers.items()}
    try:
        timestamp = int(headers["x-quiz-timestamp"])
        event_id = headers["x-quiz-event-id"]
        offered = headers["x-quiz-signature"].split(",")
    except (KeyError, ValueError):
        return "rejected"
    now = now if now is not None else time.time()
    if abs(now - timestamp) > TOLERANCE_S:
        return "rejected"
    expected = signature(secret, timestamp, body)
    if not any(hmac.compare_digest(expected, s.strip()) for s in offered):
        return "rejected"
    if event_id in seen:
        return "duplicate"
    seen.add(event_id)
    return "accepted"


def new_event_id() -> str:
    return str(uuid.uuid4())


# ---- secrets: stored encrypted, because HMAC needs the raw secret -----------


def rotate_secret(engine, fernet: Fernet, project: str) -> str:
    """Creates a new secret and returns it (shown once). The old one keeps
    signing for 24 hours."""
    import secrets

    raw = "whsec_" + secrets.token_urlsafe(32)
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(webhook_secrets)
            .where(webhook_secrets.c.project == project, webhook_secrets.c.retire_at.is_(None))
            .values(retire_at=now + ROTATION_OVERLAP)
        )
        conn.execute(
            insert(webhook_secrets).values(
                project=project, secret_encrypted=fernet.encrypt(raw.encode()), created_at=now
            )
        )
    return raw


def active_secrets(conn, fernet: Fernet, project: str) -> list[str]:
    rows = conn.execute(
        select(webhook_secrets.c.secret_encrypted)
        .where(
            webhook_secrets.c.project == project,
            or_(webhook_secrets.c.retire_at.is_(None), webhook_secrets.c.retire_at > utcnow()),
        )
        .order_by(webhook_secrets.c.created_at.desc())
    )
    return [fernet.decrypt(r.secret_encrypted).decode() for r in rows]
