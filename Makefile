VENV := .venv

# Tool paths, overridable so CI can use the ones pip put on PATH instead of a virtualenv:
#   make lint RUFF=ruff
#   make test PY=python
PY   := $(VENV)/bin/python
RUFF := $(VENV)/bin/ruff

ALL_TARGETS := dev test lint fmt probe run-ingest serve migrate verify rerender pin-base \
	image-push reset-local outbox backfill serve-bg backfill-bg ingest-loop-bg stop status logs
.PHONY: $(ALL_TARGETS)

# --- Running detached, for a box you only reach over ssh -----------------------------------
# Every background target writes a pid to var/run/<name>.pid and appends to var/log/<name>.log,
# so `make stop NAME=<name>` works from a later session that knows nothing about the first.
RUN_DIR := var/run
LOG_DIR := var/log
INGEST_INTERVAL ?= 300

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

# Probes the newest cycle `make run-ingest` stored, falling back to the test fixture when
# nothing has been ingested yet. Probing the fixture by default was worse than useless: it is a
# trimmed three-frame archive from one fixed day, so it answers a question nobody asked.
#   make probe LAT=48.15 LON=11.56
#   make probe LAT=48.15 LON=11.56 ARCHIVE=var/raw/DE1200_RV2609180745.tar.bz2
ARCHIVE_DIR ?= var/raw
ARCHIVE ?= $(firstword $(shell ls -t $(ARCHIVE_DIR)/*.tar.bz2 2>/dev/null) \
	tests/fixtures/DE1200_RV2609161355_trimmed.tar.bz2)
# A path written after the target is a *make goal*, not an argument: make tries to build it,
# says "Nothing to be done", and probe reads ARCHIVE instead - answering about a different
# archive than the one named, without saying so.
STRAY := $(filter-out $(ALL_TARGETS),$(MAKECMDGOALS))
probe:
	@test -n "$(LAT)" -a -n "$(LON)" || { \
		echo "usage: make probe LAT=48.15 LON=11.56 [ARCHIVE=path]"; exit 2; }
	@test -z "$(STRAY)" || { \
		echo "make does not pass $(STRAY) to probe - name it with ARCHIVE=$(firstword $(STRAY))"; \
		exit 2; }
	@echo "probing $(ARCHIVE)"
	@$(PY) -m rainalert.cli probe $(ARCHIVE) --lat $(LAT) --lon $(LON)

# Reads .env for configuration. Schema comes from `make migrate`, not from --create-tables:
# two ways of creating the same tables is how a schema and its migrations drift apart.
run-ingest:
	$(PY) -m rainalert.cli ingest --prune

# Binds loopback. On a remote box, prefer an SSH tunnel over HOST=0.0.0.0: this dev server has
# no TLS, a placeholder SECRET_KEY, and an open subscription form.
#   gcloud compute ssh <vm> -- -L 8000:localhost:8000     then browse http://localhost:8000
# If you do bind publicly, set PUBLIC_BASE_URL to the address you actually browse, or every
# confirmation link the app writes will point at localhost.
HOST ?= 127.0.0.1
PORT ?= 8000
serve:
	$(PY) -m uvicorn --factory rainalert.api.app:create_app --reload \
		--host $(HOST) --port $(PORT)

# The same server, detached, surviving the ssh session that started it.
#   make serve-bg HOST=0.0.0.0     then    make logs NAME=serve / make stop NAME=serve
# No --reload: the reloader runs the app in a *child* process, so killing the pid we recorded
# would leave the real server holding the port.
serve-bg:
	@$(call start_detached,serve,$(PY) -m uvicorn --factory rainalert.api.app:create_app \
		--host $(HOST) --port $(PORT))

# A full backfill is half an hour of downloading; it has no business dying with your terminal.
backfill-bg:
	@$(call start_detached,backfill,$(PY) -m rainalert.cli backfill --yes \
		$(if $(HOURS),--hours $(HOURS),) $(if $(LIMIT),--limit $(LIMIT),))

# One ingest every INGEST_INTERVAL seconds (default 300, the publication cadence). This is what
# M2's "24 h unattended" criterion needs.
ingest-loop-bg:
	@$(call start_detached,ingest-loop,env PY=$(PY) INGEST_INTERVAL=$(INGEST_INTERVAL) \
		scripts/ingest-loop.sh)

stop:
	@test -n "$(NAME)" || { echo "usage: make stop NAME=serve|backfill|ingest-loop"; exit 2; }
	@test -f $(RUN_DIR)/$(NAME).pid || { echo "$(NAME) is not running (no pid file)"; exit 1; }
	@pid=$$(cat $(RUN_DIR)/$(NAME).pid); \
	if kill -0 $$pid 2>/dev/null; then \
		kill -TERM -$$pid 2>/dev/null || kill -TERM $$pid; \
		echo "asked $(NAME) (pid $$pid) and its children to stop"; \
		for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 $$pid 2>/dev/null || break; sleep 1; done; \
		if kill -0 $$pid 2>/dev/null; then \
			kill -9 -$$pid 2>/dev/null || kill -9 $$pid; echo "had to kill -9 $$pid"; \
		fi; \
	else \
		echo "$(NAME) was not running (stale pid $$pid)"; \
	fi; \
	rm -f $(RUN_DIR)/$(NAME).pid

# One shell, not two: each recipe line gets its own, so an `exit 0` on the first would end that
# line and make would cheerfully run the next against a glob that matched nothing.
status:
	@if ! ls $(RUN_DIR)/*.pid >/dev/null 2>&1; then \
		echo "nothing started by make is running"; \
	else \
		for f in $(RUN_DIR)/*.pid; do \
			n=$$(basename $$f .pid); pid=$$(cat $$f); \
			if kill -0 $$pid 2>/dev/null; then echo "$$n  running  pid $$pid"; \
			else echo "$$n  stopped  stale pid $$pid"; fi; \
		done; \
	fi

logs:
	@test -n "$(NAME)" || { echo "usage: make logs NAME=serve|backfill|ingest-loop"; exit 2; }
	@tail -f $(LOG_DIR)/$(NAME).log

# `setsid sh -c 'echo $$ > pid; exec cmd'` puts the job in a session of its own and records the
# leader's pid - which is also the process *group* id, because exec keeps both.
#
# The group is the point. The ingest loop runs a python child per cycle; killing only the loop's
# shell leaves that child running, reparented to init, invisible to `make status` and still
# talking to DWD. Recording the group lets stop take the whole tree.
#
# All three streams are redirected: when ssh goes away the pty goes with it, and a process still
# holding it gets EIO on its next write rather than carrying on quietly. nohup covers the SIGHUP
# that arrives first.
define start_detached
	mkdir -p $(RUN_DIR) $(LOG_DIR); \
	if [ -f $(RUN_DIR)/$(1).pid ] && kill -0 $$(cat $(RUN_DIR)/$(1).pid) 2>/dev/null; then \
		echo "$(1) is already running (pid $$(cat $(RUN_DIR)/$(1).pid)); make stop NAME=$(1)"; \
		exit 1; \
	fi; \
	rm -f $(RUN_DIR)/$(1).pid; \
	nohup setsid sh -c 'echo $$$$ > $(RUN_DIR)/$(1).pid; exec $(2)' \
		>> $(LOG_DIR)/$(1).log 2>&1 < /dev/null & \
	for i in 1 2 3 4 5 6 7 8 9 10; do \
		[ -s $(RUN_DIR)/$(1).pid ] && break; sleep 0.2; \
	done; \
	sleep 1; \
	if [ -s $(RUN_DIR)/$(1).pid ] && kill -0 $$(cat $(RUN_DIR)/$(1).pid) 2>/dev/null; then \
		echo "$(1) started (pid $$(cat $(RUN_DIR)/$(1).pid)) -> $(LOG_DIR)/$(1).log"; \
	else \
		echo "$(1) exited immediately; last lines of $(LOG_DIR)/$(1).log:"; \
		tail -5 $(LOG_DIR)/$(1).log; rm -f $(RUN_DIR)/$(1).pid; exit 1; \
	fi
endef

# Fills the map timeline by fetching past cycles from DWD. Deliberately slow - one request at a
# time, 1-15 s apart - and it prints the plan and asks before it starts.
#   make backfill                 # last 12 h
#   make backfill HOURS=3         # less
#   make backfill DRY_RUN=1       # just the plan
backfill:
	@$(PY) -m rainalert.cli backfill $(if $(HOURS),--hours $(HOURS),) \
		$(if $(LIMIT),--limit $(LIMIT),) $(if $(DRY_RUN),--dry-run,) $(if $(YES),--yes,)

# Prints the links from the newest local mails, decoded. `cat`ing the .eml does not work:
# it is quoted-printable, so the token reads `=3D...` and wraps mid-string.
outbox:
	@$(PY) -m rainalert.cli outbox $(if $(N),-n $(N),)

migrate:
	$(PY) -m alembic upgrade head

verify:
	$(PY) -m rainalert.cli verify

# LIMIT=100 does only the newest N archives. An escape hatch for a machine that cannot hold a
# whole run: the newest frames are the ones anybody is looking at.
rerender:
	$(PY) -m rainalert.cli rerender $(if $(LIMIT),--limit $(LIMIT))

pin-base:
	@docker pull python:3.11-slim >/dev/null && \
	 docker inspect --format="FROM python:3.11-slim@{{index .RepoDigests 0}}" python:3.11-slim | sed "s|python:3.11-slim@python:3.11-slim|python:3.11-slim|"

image-push:
	@test -n "$(REGION)" || (echo "usage: make image-push REGION=europe-west3 PROJECT=rainchecker-195519" && exit 2)
	gcloud builds submit --tag $(REGION)-docker.pkg.dev/$(PROJECT)/rainalert/rainalert:$$(git rev-parse --short HEAD)
	@echo "deploy by digest:" && gcloud artifacts docker images describe \
	  $(REGION)-docker.pkg.dev/$(PROJECT)/rainalert/rainalert:$$(git rev-parse --short HEAD) --format="value(image_summary.fully_qualified_digest)"

# Wipes local test state. Keeps radar archives - re-fetching a full window is 577 requests to DWD.
# `make reset-local ALL=1` drops those too.
reset-local:
	$(PY) -m rainalert.cli reset-local $(if $(ALL),--all,) $(if $(YES),--yes,)
