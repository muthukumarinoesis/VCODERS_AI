# AI quiz service

A standalone service that turns a file, pasted text or a topic into a quiz.
Built from `pipeline-router-spec (4).md` (v4.1) and the quiz flow v3.

| Folder | What it is |
|---|---|
| `pipeline_router/` | Inspection and routing: file type from bytes, safety checks, page quality, formulas, language, subject, pipeline choice (spec §4–6) |
| `quiz_runner/` | Making the quiz: readers, reading pictures with Gemini, focus, generation (Gemini 2.5 Flash), checks (format, KaTeX, a second verify pass), near-duplicates and the question bank (gemini-embedding-001), export |
| `examples/` | The service: FastAPI app, Celery worker and sweeper, webhooks, rate limits, database (spec §9) |
| `examples/pipelines/`, `examples/projects.yaml` | Pipeline and client project config |
| `migrations/` | Alembic migrations (Postgres) |
| `tests/` | pytest (spec §10, plus the flow steps) |

## The flow

1. **Inspect** (`POST /v1/route`, in an isolated child process): real type from bytes, size, zip bombs, macros, encryption, damage, pages, PDF JavaScript, image size, **page quality** (under 40 is refused as `poor_quality`; 40–69 is a warning in the decision), formulas, language, subject. The same file again from the same project within 30 minutes is not inspected again (`duplicate_of`).
2. **Route**: the most specific pipeline for the input mode, language and subject that can read the file.
3. **Job** (`POST /v1/jobs`): quiz options `count`, `difficulty`, `types` and optional `focus`; corrections re-route without inspecting again.
4. **Worker**: read → pages without a usable text layer, or with formulas, are read from their pictures (poor pages skipped) → focus picks the matching parts → generate → check format and KaTeX → **verify** every answer against the material → drop near-duplicates → refill → save to the bank.
5. **Results**: `GET /v1/jobs/{id}`, a signed webhook, and `GET /v1/jobs/{id}/export?format=json|csv|gift` (GIFT is Moodle's import format).

Pipeline config keys the worker reads: `readers` (`pdf: {impl: pymupdf, formulas: picture|text}`), `quiz.types`, `quiz.locked`, `roles.generate.model`, `reading.max_ocr_pages`, `chunking.max_chars`, `verify` (default true), `dedupe.similarity`, `sources: [question_bank, generate]`, `bank.min_score`.

## Run with Docker (everything included)

```sh
cp .env.example .env            # fill in GEMINI_API_KEY and WEBHOOK_KEY; set POSTGRES_PASSWORD
docker compose up --build       # db, redis, migrate, api (127.0.0.1:8100), worker, beat
docker compose run --rm api python -m examples.admin create-key quicktest
docker compose run --rm api python -m examples.admin rotate-webhook-secret quicktest
```

The image has Tesseract (with `osd`), poppler, LibreOffice, Node/KaTeX and Tamil/Devanagari fonts.

## Run locally

```sh
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
(cd quiz_runner/katex_check && npm ci)
cp .env.example .env
.venv/bin/python -m examples.admin new-webhook-key           # put it in .env as WEBHOOK_KEY
.venv/bin/alembic upgrade head                               # Postgres schema
.venv/bin/python -m examples.admin create-key quicktest      # the client's API key, shown once
.venv/bin/uvicorn --factory examples.fastapi_app:create_app_from_env --port 8100
.venv/bin/celery -A examples.worker worker -Q quiz
.venv/bin/celery -A examples.worker beat --schedule var/celerybeat-schedule
```

Schema changes: edit `examples/db.py`, then `alembic revision --autogenerate -m "..."` and review the file.

## Tests

```sh
.venv/bin/python -m pytest                       # SQLite + fakeredis; Gemini is faked
TEST_DATABASE_URL=postgresql+psycopg://postgres:test@127.0.0.1:55432/quiz \
TEST_REDIS_URL=redis://127.0.0.1:56379/0 .venv/bin/python -m pytest     # real Postgres and Redis
docker build --build-arg REQUIREMENTS=requirements-dev.txt -t vcoders/quiz-service:test .
docker run --rm vcoders/quiz-service:test python -m pytest  # with Tesseract, pdftoppm, LibreOffice
```

The Postgres run drops and recreates the tables in that database: point it at a test database only.

Local test containers: `docker run -d --name vcoders-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=quiz -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16` and `docker run -d --name vcoders-redis -p 127.0.0.1:56379:6379 redis:7-alpine`.

## Still open

- A real Gemini run (needs `GEMINI_API_KEY`), to check prompts, embedding sizes and the similarity thresholds.
- Page-quality thresholds were tuned on synthetic pages; tune them from real uploads.
- Encryption markers for `.doc`/`.xls`/`.ppt` (spec §12.12) need real password-protected samples from Office.
- PyMuPDF is AGPL-licensed: confirm the licence, or switch the PDF reader to pypdf.
