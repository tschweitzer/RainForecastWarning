#!/bin/sh
# One ingest every INGEST_INTERVAL seconds, forever.
#
# This exists as a file rather than inline in the Makefile because the Makefile wraps background
# jobs in `sh -c '...'` to give them their own process group, and a loop with its own quoting
# nested inside that is unreadable at best and wrong at worst.
#
# It never exits on a failed cycle. A cycle can fail for reasons that clear themselves - DWD
# briefly unreachable, a publication running late - and a loop that stops on the first of those
# is a loop that is not running when the rain comes.
set -u

PY="${PY:-.venv/bin/python}"
INTERVAL="${INGEST_INTERVAL:-300}"

echo "ingest loop starting, every ${INTERVAL}s, using ${PY}"
while true; do
    date -u "+%Y-%m-%dT%H:%M:%SZ"
    "$PY" -m rainalert.cli ingest --prune || echo "cycle failed; continuing"
    sleep "$INTERVAL"
done
