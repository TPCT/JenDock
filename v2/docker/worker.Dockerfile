# syntax=docker/dockerfile:1
FROM python:3.13.9-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml requirements.lock ./
COPY backend ./backend
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -c requirements.lock .

FROM python:3.13.9-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
# git clones the pinned commit of each run (over ssh with the deploy key, hence openssh-client);
# the pinned docker CLI talks to the host daemon.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker:27.5.1-cli /usr/local/bin/docker /usr/local/bin/docker
# The compose plugin of the same image: container env edits recreate single services with it.
COPY --from=docker:27.5.1-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
# Pinned GitHub host key: the ssh transport only trusts this file (StrictHostKeyChecking=yes).
COPY docker/github_known_hosts /etc/jendock/github_known_hosts
# The worker user needs access to the Docker socket; compose adds the host docker GID via group_add.
RUN groupadd --system --gid 10001 jendock \
    && useradd --system --uid 10001 --gid jendock --home-dir /var/lib/jendock jendock \
    && mkdir -p /var/lib/jendock /run/jendock /run/jendock-keys \
    && chown jendock:jendock /var/lib/jendock /run/jendock /run/jendock-keys \
    && chmod 2770 /var/lib/jendock /run/jendock \
    && chmod 0700 /run/jendock-keys
# /run/jendock-keys holds the secret key: a worker-only volume, never mounted in the API.
COPY --from=builder /opt/venv /opt/venv
USER jendock
STOPSIGNAL SIGTERM
CMD ["python", "-m", "backend.worker"]
