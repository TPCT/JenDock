# syntax=docker/dockerfile:1

# The web UI is built once here and served by the API itself (JENDOCK_FRONTEND_DIST).
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
COPY backend ./backend
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -c requirements.lock .

FROM python:3.13.9-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
# The API runs as uid 10002 and the worker as uid 10001; both are in group gid 10001, which
# owns the shared data and socket directories (setgid, group-writable, closed to others).
RUN groupadd --system --gid 10001 jendock \
    && useradd --system --uid 10002 --gid jendock --home-dir /var/lib/jendock jendockapi \
    && mkdir -p /var/lib/jendock /run/jendock \
    && chown 10001:10001 /var/lib/jendock /run/jendock \
    && chmod 2770 /var/lib/jendock /run/jendock
COPY --from=builder /opt/venv /opt/venv
COPY --from=frontend /build/dist /opt/jendock/frontend
ENV JENDOCK_FRONTEND_DIST=/opt/jendock/frontend
USER jendockapi
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
# Proxy headers are trusted only for JENDOCK_TRUSTED_PROXIES (default: nobody).
CMD ["python", "-m", "backend.api"]
