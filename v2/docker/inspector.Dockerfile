# syntax=docker/dockerfile:1
# The database inspector's helper image: the worker starts one short-lived container from it per
# operation, attached to the environment's compose network (never the worker's own network).
# It holds the database drivers and the inspector package only: no Docker CLI, no git, no key
# file, none of the platform. Build:  docker compose --profile tools build inspector
FROM python:3.13.9-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.lock ./
# Only the two drivers (and what they need), pinned by the same lock file as the platform.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -c requirements.lock "psycopg[binary]" PyMySQL

FROM python:3.13.9-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app PATH=/opt/venv/bin:$PATH
COPY --from=builder /opt/venv /opt/venv
COPY shiftcodes/__init__.py /app/shiftcodes/__init__.py
COPY shiftcodes/inspector /app/shiftcodes/inspector
# The worker also passes --user 65534 --read-only --cap-drop ALL; the user is set here as well.
USER 65534:65534
WORKDIR /app
CMD ["python", "-m", "shiftcodes.inspector.helper"]
