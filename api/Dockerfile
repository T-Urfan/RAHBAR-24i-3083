# RAHBAR api: reads the index, never writes it. Build context: api/
#   docker build -t rahbar-api:dev api
FROM python:3.12.10-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /venv
COPY requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.12.10-slim-bookworm
# Security fixes from Debian that are newer than the base image (Trivy gate in CI).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 rahbar && useradd --uid 10001 --gid 10001 --no-create-home rahbar
ENV PATH=/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /venv /venv
WORKDIR /app
COPY app ./app
USER 10001:10001
EXPOSE 8000
# exec form: uvicorn is PID 1 and receives SIGTERM, then drains for up to 20 s
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20", "--no-access-log"]
