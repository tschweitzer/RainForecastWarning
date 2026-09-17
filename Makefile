VENV := .venv

.PHONY: dev test lint fmt probe

dev:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e '.[dev]'

test:
	$(VENV)/bin/python -m pytest -q

lint:
	$(VENV)/bin/ruff check rainalert tests
	$(VENV)/bin/ruff format --check rainalert tests

fmt:
	$(VENV)/bin/ruff format rainalert tests
	$(VENV)/bin/ruff check --fix rainalert tests

# Example: make probe LAT=50.11 LON=8.68
probe:
	$(VENV)/bin/python -m rainalert.cli probe tests/fixtures/DE1200_RV2609161355_trimmed.tar.bz2 \
		--lat $(LAT) --lon $(LON)

run-ingest:
	ARCHIVE_DIR=$(or $(ARCHIVE_DIR),./var/raw) \
	$(VENV)/bin/python -m rainalert.cli ingest --create-tables --prune

serve:
	$(VENV)/bin/uvicorn --factory rainalert.api.app:create_app --reload --port 8000

migrate:
	$(VENV)/bin/alembic upgrade head

verify:
	$(VENV)/bin/python -m rainalert.cli verify

rerender:
	$(VENV)/bin/python -m rainalert.cli rerender

pin-base:
	@docker pull python:3.11-slim >/dev/null && \
	 docker inspect --format="FROM python:3.11-slim@{{index .RepoDigests 0}}" python:3.11-slim | sed "s|python:3.11-slim@python:3.11-slim|python:3.11-slim|"

image-push:
	@test -n "$(REGION)" || (echo "usage: make image-push REGION=europe-west3 PROJECT=rainchecker-195519" && exit 2)
	gcloud builds submit --tag $(REGION)-docker.pkg.dev/$(PROJECT)/rainalert/rainalert:$$(git rev-parse --short HEAD)
	@echo "deploy by digest:" && gcloud artifacts docker images describe \
	  $(REGION)-docker.pkg.dev/$(PROJECT)/rainalert/rainalert:$$(git rev-parse --short HEAD) --format="value(image_summary.fully_qualified_digest)"
