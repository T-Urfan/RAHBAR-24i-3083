# RAHBAR indexer: the Job's image, the only writer to Chroma. Build context: the REPO ROOT,
# so the image can carry the corpus fetch script, its source list and the CI fixtures:
#   docker build -f indexer/Dockerfile -t rahbar-indexer:dev .
# (indexer/Dockerfile.dockerignore applies to this build.)
FROM python:3.12.10-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /venv
COPY indexer/requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.12.10-slim-bookworm
# tesseract-ocr: OCR for pages with no text layer (the 2023 rules, the start-up policy).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 rahbar && useradd --uid 10001 --gid 10001 --no-create-home rahbar
ENV PATH=/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp CORPUS_DIR=/corpus
COPY --from=build /venv /venv
WORKDIR /app
COPY indexer/pipeline ./pipeline
COPY corpus/fetch.py corpus/sources.yaml ./corpus/
# only the three fixture documents: the README and golden set must not be indexed
COPY eval/fixtures/academic-rules-2023.md eval/fixtures/academic-rules-2026.md eval/fixtures/fees-2026.md ./fixtures/
USER 10001:10001
# exec form: SIGTERM reaches Python, which exits 143 and never leaves a half-built index live
ENTRYPOINT ["python", "-m", "pipeline.run"]
