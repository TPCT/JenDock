BIN := .venv/bin

.PHONY: install lint format typecheck test test-docker test-e2e test-py313 lock check migrate lint-jenkins preflight

PY_IMAGE := python:3.13.9-slim

install:
	$(BIN)/pip install -e '.[dev]'

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy backend tests

test:
	$(BIN)/pytest

test-docker:
	$(BIN)/pytest -m docker -p no:cacheprovider

migrate:
	$(BIN)/python -m backend.repositories.migrate

lock:
	docker run --rm -v "$(CURDIR):/src:ro" $(PY_IMAGE) sh -c \
	  'cp -r /src /tmp/w && cd /tmp/w && rm -rf .venv && python -m venv /tmp/v \
	  && /tmp/v/bin/pip install -q . && /tmp/v/bin/pip freeze --exclude jendock' \
	  | grep -v '^\[notice\]' > requirements.lock

test-py313:
	docker run --rm -v "$(CURDIR):/src:ro" $(PY_IMAGE) sh -c \
	  '(apt-get update -qq && apt-get install -y -qq --no-install-recommends git) >/dev/null \
	  && cp -r /src /tmp/w && cd /tmp/w && rm -rf .venv && python -m venv /tmp/v \
	  && /tmp/v/bin/pip install -q -c requirements.lock ".[dev]" && /tmp/v/bin/pytest -p no:cacheprovider'

check: lint typecheck test

lint-jenkins:
	scripts/jenkins_lint.sh

# Read-only host check before the production installation (docs/deployment.md); extra flags via PREFLIGHT_ARGS.
preflight:
	python3 scripts/preflight.py $(PREFLIGHT_ARGS)

# Whole platform with real containers (builds the images from the tree; several minutes).
test-e2e:
	$(BIN)/pytest -m e2e -p no:cacheprovider tests/e2e
