VENV := .venv

# Tool paths, overridable so CI can use the ones pip put on PATH instead of a virtualenv:
#   make lint RUFF=ruff
#   make test PY=python
PY   := $(VENV)/bin/python
RUFF := $(VENV)/bin/ruff

ALL_TARGETS := dev test lint fmt probe run-ingest serve migrate verify rerender pin-base \
	image-push reset-local outbox backfill serve-bg backfill-bg ingest-loop-bg stop status logs \
	verify-basemap vendor-leaflet
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
# The job runs at nice 10 by itself. PAUSE=0.5 additionally idles between archives, which is
# what keeps a shared-core VM responsive - nice only arbitrates between local processes, it
# cannot stop the hypervisor throttling a box that has burned its CPU allowance.
rerender:
	$(PY) -m rainalert.cli rerender $(if $(LIMIT),--limit $(LIMIT)) $(if $(PAUSE),--pause $(PAUSE))

pin-base:
	@docker pull python:3.11-slim >/dev/null && \
	 docker inspect --format="FROM python:3.11-slim@{{index .RepoDigests 0}}" python:3.11-slim | sed "s|python:3.11-slim@python:3.11-slim|python:3.11-slim|"

# Building is only half a deploy. Cloud Run is pinned to a digest (infra: var.image), so an
# apply that still names the previous one redeploys the previous code and reports success - the
# quietest possible way to spend twenty minutes wondering why a fix did nothing. So this prints
# the tfvars line rather than the bare digest: the next step is a paste, not a transcription.
#
# It also warns about uncommitted changes, because `gcloud builds submit` uploads the working
# directory while the tag comes from HEAD. That builds what you have and labels it with the last
# commit, which makes the tag a lie about what is running.
image-push:
	@test -n "$(REGION)" || (echo "usage: make image-push REGION=europe-west3 PROJECT=rainchecker-195519" && exit 2)
	@git diff --quiet HEAD || echo "WARNING: uncommitted changes will be built and tagged $$(git rev-parse --short HEAD)"
	gcloud builds submit --tag $(REGION)-docker.pkg.dev/$(PROJECT)/rainalert/rainalert:$$(git rev-parse --short HEAD)
	@digest=$$(gcloud artifacts docker images describe \
	   $(REGION)-docker.pkg.dev/$(PROJECT)/rainalert/rainalert:$$(git rev-parse --short HEAD) \
	   --format="value(image_summary.fully_qualified_digest)") && \
	 echo && echo "Put this in infra/terraform.tfvars, then apply - nothing deploys until you do:" && \
	 echo && echo "image = \"$$digest\"" && echo

# Wipes local test state. Keeps radar archives - re-fetching a full window is 577 requests to DWD.
# `make reset-local ALL=1` drops those too.
reset-local:
	$(PY) -m rainalert.cli reset-local $(if $(ALL),--all,) $(if $(YES),--yes,)

# --- Basemap and vendored assets -----------------------------------------------------------
# LEAFLET_SHA512 is the npm registry's own integrity value for leaflet@1.9.4. Vendoring is only
# worth anything if what landed is what upstream published, so this checks rather than trusts.
LEAFLET_VERSION := 1.9.4
LEAFLET_SHA512  := nxS1ynzJOmOlHp+iL3FyWqK89GtNL8U8rvlMOsQdTTssxZwCXh8N2NB3GDQOL+YR3XnWyZAxwQixURb+FA74PA==
LEAFLET_DIR     := rainalert/api/static/vendor/leaflet

# The basemap default is a WMTS path, and a wrong WMTS path does not error - it serves blank tiles,
# which looks exactly like "no rain anywhere" on a map whose whole job is showing rain. One request
# settles it. Needs plain outbound access; the sandbox this was written in could not reach
# sgx.geodatenzentrum.de at all, which is the whole reason the target exists.
verify-basemap:
	@url=$$($(PY) -c 'from rainalert.config import Settings; print(Settings().map_tile_url)'); \
	if [ -z "$$url" ]; then \
	  echo "no basemap configured (MAP_TILE_URL is empty) - nothing to verify"; exit 0; \
	fi; \
	probe=$$(printf '%s' "$$url" | sed -e 's/{z}/8/' -e 's/{y}/86/' -e 's/{x}/135/' -e 's/{s}/a/' -e 's/{r}//'); \
	echo "GET $$probe"; \
	out=$$(curl -sS -o /dev/null -m 30 -w '%{http_code} %{content_type}' "$$probe") || exit 1; \
	echo "  -> $$out"; \
	case "$$out" in \
	  "200 image/"*) echo "OK - tiles are being served" ;; \
	  *) echo "NOT OK - expected 200 and an image/* content type."; \
	     echo "  For a WMTS provider, check the {z}/{y}/{x} order and the tile matrix set."; \
	     exit 1 ;; \
	esac

# Refreshes the vendored Leaflet from the npm registry - the same bytes unpkg serves, but with a
# checksum to verify them against. Writes nothing unless the tarball matches LEAFLET_SHA512.
# See $(LEAFLET_DIR)/README.md; after a version bump update the hashes there and in
# tests/test_vendored_leaflet.py.
vendor-leaflet:
	@set -e; \
	tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	url="https://registry.npmjs.org/leaflet/-/leaflet-$(LEAFLET_VERSION).tgz"; \
	echo "fetching $$url"; \
	curl -sSf -m 120 -o "$$tmp/leaflet.tgz" "$$url"; \
	got=$$($(PY) -c 'import base64,hashlib,sys; print("sha512-"+base64.b64encode(hashlib.sha512(open(sys.argv[1],"rb").read()).digest()).decode())' "$$tmp/leaflet.tgz"); \
	if [ "$$got" != "sha512-$(LEAFLET_SHA512)" ]; then \
	  echo "CHECKSUM MISMATCH - nothing written"; \
	  echo "  expected sha512-$(LEAFLET_SHA512)"; \
	  echo "  got      $$got"; \
	  exit 1; \
	fi; \
	echo "checksum ok"; \
	tar xzf "$$tmp/leaflet.tgz" -C "$$tmp"; \
	mkdir -p $(LEAFLET_DIR)/images; \
	cp "$$tmp/package/dist/leaflet.js" "$$tmp/package/dist/leaflet.css" $(LEAFLET_DIR)/; \
	cp "$$tmp/package/LICENSE" $(LEAFLET_DIR)/LICENSE; \
	for i in layers.png layers-2x.png marker-icon.png; do \
	  cp "$$tmp/package/dist/images/$$i" $(LEAFLET_DIR)/images/$$i; \
	done; \
	echo "vendored Leaflet $(LEAFLET_VERSION) into $(LEAFLET_DIR)"; \
	sha256sum $(LEAFLET_DIR)/leaflet.js $(LEAFLET_DIR)/leaflet.css
