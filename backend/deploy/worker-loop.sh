#!/bin/sh
set -u

ready_file=/tmp/agentic-os-worker-ready
poll_seconds="${AGENTIC_OS_WORKER_POLL_SECONDS:-2}"

rm -f "$ready_file"
while true; do
    if agentic-os worker run-once --worker-id "${AGENTIC_OS_WORKER_ID:-compose-worker}"; then
        date -u +%Y-%m-%dT%H:%M:%SZ > "$ready_file"
    else
        rm -f "$ready_file"
    fi
    sleep "$poll_seconds"
done
