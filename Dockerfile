# One image for the API, the worker and beat. It carries everything the
# spec needs at runtime: Tesseract with script detection (osd), poppler's
# pdftoppm, LibreOffice for DOC/PPT/XLS/ODP/ODS, and Node for KaTeX checks.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-osd poppler-utils \
      libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
      nodejs npm fonts-dejavu-core fonts-lohit-taml fonts-lohit-deva \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ARG REQUIREMENTS=requirements.txt
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r ${REQUIREMENTS}

COPY quiz_runner/katex_check/package.json quiz_runner/katex_check/package-lock.json quiz_runner/katex_check/
RUN cd quiz_runner/katex_check && npm ci --omit=dev --no-audit --no-fund

COPY . .
RUN useradd --create-home --uid 10001 quiz && mkdir -p var/storage && chown -R quiz var
USER quiz
ENV PYTHONUNBUFFERED=1 HOME=/home/quiz

EXPOSE 8100
CMD ["uvicorn", "--factory", "examples.fastapi_app:create_app_from_env", "--host", "0.0.0.0", "--port", "8100", "--workers", "2"]
