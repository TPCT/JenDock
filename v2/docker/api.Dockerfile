# syntax=docker/dockerfile:1

# The web UI is built once here and served by the API itself (SHIFTCODES_FRONTEND_DIST).
FROM node:22.23.3-alpine3.24 AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.13.9-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml requirements.lock ./
COPY shiftcodes ./shiftcodes
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -c requirements.lock .

FROM python:3.13.9-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
# The API runs as uid 10002 and the worker as uid 10001; both are in group gid 10001, which
# owns the shared data and socket directories (setgid, group-writable, closed to others).
RUN groupadd --system --gid 10001 shiftcodes \
    && useradd --system --uid 10002 --gid shiftcodes --home-dir /var/lib/shiftcodes shiftcodesapi \
    && mkdir -p /var/lib/shiftcodes /run/shiftcodes \
    && chown 10001:10001 /var/lib/shiftcodes /run/shiftcodes \
    && chmod 2770 /var/lib/shiftcodes /run/shiftcodes
COPY --from=builder /opt/venv /opt/venv
COPY --from=frontend /build/dist /opt/shiftcodes/frontend
ENV SHIFTCODES_FRONTEND_DIST=/opt/shiftcodes/frontend
USER shiftcodesapi
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
# Proxy headers are trusted only for SHIFTCODES_TRUSTED_PROXIES (default: nobody).
CMD ["python", "-m", "shiftcodes.api"]
