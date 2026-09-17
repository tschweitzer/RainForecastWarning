VENV := .venv

# Tool paths, overridable so CI can use the ones pip put on PATH instead of a virtualenv:
#   make lint RUFF=ruff
#   make test PY=python
PY   := $(VENV)/bin/python
RUFF := $(VENV)/bin/ruff

.PHONY: dev test lint fmt probe run-ingest serve migrate verify rerender pin-base image-push reset-local

dev:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e '.[dev]'

test:
	$(PY) -m pytest -q

lint:
	$(RUFF) check rainalert tests
	$(RUFF) format --check rainalert tests

fmt:
	$(RUFF) format rainalert tests
	$(RUFF) check --fix rainalert tests

# Example: make probe LAT=50.11 LON=8.68
probe:
	$(PY) -m rainalert.cli probe tests/fixtures/DE1200_RV2609161355_trimmed.tar.bz2 \
		--lat $(LAT) --lon $(LON)

# Reads .env for configuration. Schema comes from `make migrate`, not from --create-tables:
# two ways of creating the same tables is how a schema and its migrations drift apart.
run-ingest:
	$(PY) -m rainalert.cli ingest --prune

serve:
	$(PY) -m uvicorn --factory rainalert.api.app:create_app --reload --port 8000

migrate:
	$(PY) -m alembic upgrade head

verify:
	$(PY) -m rainalert.cli verify

rerender:
	$(PY) -m rainalert.cli rerender

pin-base:
	@docker pull python:3.11-slim >/dev/null && \
	 docker inspect --format="FROM python:3.11-slim@{{index .RepoDigests 0}}" python:3.11-slim | sed "s|python:3.11-slim@python:3.11-slim|python:3.11-slim|"

image-push:
	@test -n "$(REGION)" || (echo "usage: make image-push REGION=europe-west3 PROJECT=rainchecker-195519" && exit 2)
	gcloud builds submit --tag $(REGION)-docker.pkg.dev/$(PROJECT)/rainalert/rainalert:$$(git rev-parse --short HEAD)
	@echo "deploy by digest:" && gcloud artifacts docker images describe \
	  $(REGION)-docker.pkg.dev/$(PROJECT)/rainalert/rainalert:$$(git rev-parse --short HEAD) --format="value(image_summary.fully_qualified_digest)"

# Wipes local test state. Keeps radar archives - re-fetching them is 144 requests to DWD.
# `make reset-local ALL=1` drops those too.
reset-local:
	$(PY) -m rainalert.cli reset-local $(if $(ALL),--all,) $(if $(YES),--yes,)
