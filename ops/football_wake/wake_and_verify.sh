#!/bin/sh
# Wake the Football service and prove a useful scheduler cycle completed.
#
# Usage:
#   wake_and_verify.sh [base_url] [max_wait_seconds] [poll_seconds]
#
# Exit codes:
#   0  HTTP callback succeeded and /health/wake verified a useful cycle
#   1  callback failed, cycle failed, or verification timed out
#
# This script never calls a data provider. /health and /health/wake are
# read-only application endpoints; provider work happens only in the app's
# scheduler process after the container wakes.

set -eu

BASE_URL="${1:-https://sports-edge-ai-production.up.railway.app}"
MAX_WAIT_SECONDS="${2:-300}"
POLL_SECONDS="${3:-15}"
SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "wake-start ts=${SINCE} target=${BASE_URL}"

# Do not emit potentially sensitive response bodies or headers to public
# build/deploy logs. The transport failure is visible by exit status.
if ! wget -q -T 90 -O /dev/null "${BASE_URL}/health"; then
    echo "wake-http-failed"
    exit 1
fi
echo "wake-http-ok"

ELAPSED=0
while [ "${ELAPSED}" -lt "${MAX_WAIT_SECONDS}" ]; do
    wget -q -T 30 -O /dev/null "${BASE_URL}/health/wake?since=${SINCE}" && WAKE_RC=0 || WAKE_RC=$?
    echo "wake-poll elapsed_seconds=${ELAPSED} http_rc=${WAKE_RC}"
    if [ "${WAKE_RC}" -eq 0 ]; then
        echo "wake-ok"
        exit 0
    fi
    sleep "${POLL_SECONDS}"
    ELAPSED=$((ELAPSED + POLL_SECONDS))
done

echo "wake-failed reason=cycle-not-verified timeout_seconds=${MAX_WAIT_SECONDS}"
exit 1
