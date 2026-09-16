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
