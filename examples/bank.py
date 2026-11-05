"""The question bank: saved questions, found again by meaning (spec 6:
topic pipelines use the question bank first, then generate)."""

import json

from sqlalchemy import Float, cast, insert, literal, select

from quiz_runner.embeddings import cosine

from . import db

PYTHON_SCAN_LIMIT = 5000


class QuestionBank:
    def __init__(self, engine):
        self.engine = engine

    def add(self, job: dict, questions: list[dict], vectors: list[list[float]]) -> None:
        if not questions:
            return
        decision, now = job.get("decision") or {}, db.utcnow()
        rows = [
            dict(id=db.new_id(), project=job["project"], language=decision.get("language"),
                 subject=decision.get("subject"), type=q["type"], difficulty=q["difficulty"],
                 question=q, embedding=v, job_id=job["id"], created_at=now)
            for q, v in zip(questions, vectors)
        ]  # fmt: skip
        with self.engine.begin() as conn:
            conn.execute(insert(db.bank_questions), rows)

    def find(
        self, project, language, subject, types, vector, limit, min_score
    ) -> list[tuple[dict, list, float]]:
        """(question, embedding, score), best first, all at least min_score.
        Same project and language; same subject when one is given."""
        t = db.bank_questions
        where = [
            t.c.project == project,
            t.c.language.is_not_distinct_from(language),
            t.c.type.in_(list(types)),
        ]
        if subject:
            where.append(t.c.subject == subject)
        with self.engine.connect() as conn:
            if conn.dialect.name == "postgresql":
                from pgvector.sqlalchemy import Vector

                distance = t.c.embedding.op("<=>", return_type=Float)(
                    cast(literal(json.dumps(vector)), Vector(len(vector)))
                )
                rows = conn.execute(
                    select(t.c.question, t.c.embedding, distance.label("distance"))
                    .where(*where)
                    .order_by(distance)
                    .limit(limit)
                ).all()
                found = [(r.question, r.embedding, 1 - float(r.distance)) for r in rows]
            else:
                # shortcut: SQLite (dev, tests) scans the newest rows in Python;
                # Postgres searches the HNSW index.
                rows = conn.execute(
                    select(t.c.question, t.c.embedding).where(*where).order_by(t.c.created_at.desc())
                    .limit(PYTHON_SCAN_LIMIT)
                ).all()  # fmt: skip
                found = sorted(((r.question, r.embedding, cosine(vector, r.embedding)) for r in rows),
                               key=lambda f: f[2], reverse=True)[:limit]  # fmt: skip
        return [f for f in found if f[2] >= min_score]
