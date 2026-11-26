#!/usr/bin/env bash
#
# verify-all.sh -- the single command that proves the whole platform works.
#
# It runs, in order:
#
#   1. the 49-check simulator verifier suite;
#   2. the backend test suite (pytest, in-container);
#   3. the frontend test suite (vitest, in-container);
#   4. the observability self-check -- /metrics carries every required
#      metric family and /api/health carries the PDF section 16 fields;
#   5. the load test (only with --with-load-test, since it is slow and it
#      deliberately puts the backend under load).
#
# Every suite's result lands in one JSON object, printed to stdout and
# optionally written to a file. The exit code is 0 only if every suite
# passed.
#
# Usage:
#   ./scripts/verify-all.sh [--json OUT.json] [--with-load-test]
#                           [--load-duration SECONDS] [--load-vus N]
#                           [--skip-frontend] [--skip-load-test]
#
# The JSON is deterministic in *shape* (the same keys, always) so a harness
# can parse it without guessing. The *values* -- durations, counts -- are
# measurements, and they differ from run to run by nature.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

JSON_OUT=""
RUN_LOAD_TEST=0
LOAD_DURATION=30
LOAD_VUS=10
SKIP_FRONTEND=0
SKIP_LOAD_TEST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)            JSON_OUT="${2:-}"; shift 2 ;;
    --with-load-test)  RUN_LOAD_TEST=1; shift ;;
    --load-duration)   LOAD_DURATION="${2:-30}"; shift 2 ;;
    --load-vus)        LOAD_VUS="${2:-10}"; shift 2 ;;
    --skip-frontend)   SKIP_FRONTEND=1; shift ;;
    --skip-load-test)  SKIP_LOAD_TEST=1; shift ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2 ;;
  esac
done

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START_EPOCH=$(date +%s)

# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  BOLD=""; RED=""; GREEN=""; YELLOW=""; DIM=""; RESET=""
fi

say()  { printf '%s\n' "$*"; }
head2() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '%s  PASS%s  %s\n' "$GREEN" "$RESET" "$*"; }
bad()  { printf '%s  FAIL%s  %s\n' "$RED" "$RESET" "$*"; }
warn() { printf '%s  SKIP%s  %s\n' "$YELLOW" "$RESET" "$*"; }
note() { printf '%s        %s%s\n' "$DIM" "$*" "$RESET"; }

# --------------------------------------------------------------------------- #
# Compose helpers
#
# The stack must be up for the verifier, the observability self-check and
# the load test to mean anything. The backend image has no bind-mount (the
# project path contains spaces), so the code is baked in -- meaning the
# backend must be *rebuilt* for the test suites to run against the current
# source. That is why this script builds rather than assuming.
# --------------------------------------------------------------------------- #
COMPOSE=(docker compose)

compose() { "${COMPOSE[@]}" "$@"; }

ensure_stack() {
  head2 "Bringing the stack up"
  # ALL FIVE services. The verifier suite runs
  # `docker compose exec -T client-verifier ...`, so `client-verifier` must be
  # running -- `exec` refuses a stopped container. It depends only on
  # `simulator-api` and `redis`, so naming it explicitly is enough.
  if ! compose up -d --build simulator-api redis backend frontend client-verifier >/tmp/verify-all-compose.log 2>&1; then
    note "compose log: /tmp/verify-all-compose.log"
    return 1
  fi
  # Wait for the backend's healthcheck, not just "the container started".
  local waited=0
  while (( waited < 90 )); do
    if curl -fsS -m 3 http://localhost:8002/api/health >/dev/null 2>&1; then
      note "the backend is healthy"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  note "the backend did not become healthy within 90s"
  return 1
}

# --------------------------------------------------------------------------- #
# Suite 1: the simulator verifier (the 49 checks)
# --------------------------------------------------------------------------- #
SUITE_VERIFIER="skipped"
VERIFIER_OUTPUT=""
VERIFIER_SUMMARY=""

run_verifier() {
  head2 "Suite 1/5 -- the simulator verifier"
  local log=/tmp/verify-all-verifier.log
  if compose exec -T client-verifier python verify_simulator.py \
      --base-url http://simulator-api:8000 \
      --redis-url redis://redis:6379/0 \
      --json-report /app/reports/verify-all.json \
      >"$log" 2>&1; then
    SUITE_VERIFIER="passed"
    ok "all simulator checks passed"
    VERIFIER_OUTPUT="$log"
    # The summary line is "ALL 49 CHECKS PASSED". The per-group ratios
    # (10/10, 7/7, ...) are NOT the total -- there are six groups, and the
    # last one alone would report "4/4".
    VERIFIER_SUMMARY="$(grep -Eo '[0-9]+ CHECKS PASSED' "$log" | tail -1)"
    VERIFIER_SUMMARY="${VERIFIER_SUMMARY// CHECKS PASSED/ checks passed}"
  else
    SUITE_VERIFIER="failed"
    bad "the simulator verifier reported failures"
    VERIFIER_OUTPUT="$log"
    VERIFIER_SUMMARY="$(grep -Eo '[0-9]+/[0-9]+' "$log" | tail -1)"
    tail -30 "$log" | sed 's/^/        /'
  fi
}

# --------------------------------------------------------------------------- #
# Suite 2: the backend tests
# --------------------------------------------------------------------------- #
SUITE_BACKEND="skipped"
BACKEND_SUMMARY=""

run_backend() {
  head2 "Suite 2/5 -- the backend test suite"
  local log=/tmp/verify-all-backend.log
  if compose exec -T backend python -m pytest tests/ -q >"$log" 2>&1; then
    SUITE_BACKEND="passed"
    # pytest's last line: "457 passed, 8 skipped, 7 warnings in 82.08s"
    BACKEND_SUMMARY="$(grep -E '^[0-9]+ passed' "$log" | tail -1)"
    ok "the backend suite passed  ${BACKEND_SUMMARY}"
  else
    SUITE_BACKEND="failed"
    BACKEND_SUMMARY="$(grep -E '^[0-9]+ passed' "$log" | tail -1)"
    bad "the backend suite failed  ${BACKEND_SUMMARY}"
    tail -40 "$log" | sed 's/^/        /'
  fi
}

# --------------------------------------------------------------------------- #
# Suite 3: the frontend tests
# --------------------------------------------------------------------------- #
SUITE_FRONTEND="skipped"
FRONTEND_SUMMARY=""

run_frontend() {
  head2 "Suite 3/5 -- the frontend test suite"
  if [[ $SKIP_FRONTEND -eq 1 ]]; then
    SUITE_FRONTEND="skipped"
    warn "skipped on request"
    return
  fi
  local log=/tmp/verify-all-frontend.log
  # The frontend's *runtime* image is deliberately slim -- it carries the
  # built assets and the proxy server, and no toolchain, no source and no
  # package.json. The suite therefore cannot run in it.
  #
  # So it runs in the Dockerfile's `test` stage: a build target that has the
  # toolchain and the source. Building it and running it in a throwaway
  # container keeps the runtime image slim AND makes the harness
  # independent of whether the host has Node installed.
  local image="buetcsefest-frontend-test:verify"
  if ! docker build \
        --target test \
        -t "$image" \
        -f frontend/Dockerfile \
        . >"$log" 2>&1; then
    SUITE_FRONTEND="failed"
    FRONTEND_SUMMARY="the test image failed to build"
    bad "the frontend suite failed  ${FRONTEND_SUMMARY}"
    tail -40 "$log" | sed 's/^/        /'
    return
  fi

  if docker run --rm "$image" >>"$log" 2>&1; then
    SUITE_FRONTEND="passed"
    # vitest prints "  Tests  136 passed | 3 skipped (139)" -- the count is
    # followed by a pipe, so the summary is taken from the Tests line and
    # the whole "passed | skipped" tail is kept.
    FRONTEND_SUMMARY="$(grep -E '^\s+Tests\s+' "$log" | tail -1 | sed 's/^[[:space:]]*//')"
    ok "the frontend suite passed  ${FRONTEND_SUMMARY}"
  else
    SUITE_FRONTEND="failed"
    FRONTEND_SUMMARY="$(grep -E '^\s+Tests\s+' "$log" | tail -1 | sed 's/^[[:space:]]*//')"
    bad "the frontend suite failed  ${FRONTEND_SUMMARY}"
    tail -40 "$log" | sed 's/^/        /'
  fi
}

# --------------------------------------------------------------------------- #
# Suite 4: the observability self-check
#
# The PDF's section 15 requires four observability layers and section 16
# requires a component health table with p95 latency and an error rate. This
# check asserts that the endpoints actually carry them -- not that some
# other code path would.
# --------------------------------------------------------------------------- #
SUITE_OBSERVABILITY="skipped"
OBSERVABILITY_DETAIL=""

run_observability() {
  head2 "Suite 4/5 -- the observability self-check"
  local log=/tmp/verify-all-observability.log
  if python3 scripts/check_observability.py --base-url http://localhost:8002 >"$log" 2>&1; then
    SUITE_OBSERVABILITY="passed"
    ok "the metrics endpoint and the health table carry every required field"
    OBSERVABILITY_DETAIL="$(grep -Eo 'checked [0-9]+ assertions' "$log" | tail -1)"
  else
    SUITE_OBSERVABILITY="failed"
    bad "the observability self-check failed"
    OBSERVABILITY_DETAIL="$(grep -cE '^  (FAIL|ok)' "$log" 2>/dev/null)"
    tail -40 "$log" | sed 's/^/        /'
  fi
}

# --------------------------------------------------------------------------- #
# Suite 5: the load test
# --------------------------------------------------------------------------- #
SUITE_LOADTEST="skipped"
LOADTEST_REPORT=""

run_loadtest() {
  head2 "Suite 5/5 -- the load test"
  if [[ $SKIP_LOAD_TEST -eq 1 ]]; then
    SUITE_LOADTEST="skipped"
    warn "skipped on request"
    return
  fi
  if [[ $RUN_LOAD_TEST -ne 1 ]]; then
    SUITE_LOADTEST="skipped"
    warn "skipped (pass --with-load-test to run it)"
    return
  fi
  local log=/tmp/verify-all-loadtest.log
  if ./loadtest/run.sh --duration "$LOAD_DURATION" --vus "$LOAD_VUS" >"$log" 2>&1; then
    SUITE_LOADTEST="passed"
    LOADTEST_REPORT="$(ls -t loadtest/results/*.json 2>/dev/null | head -1)"
    ok "the load test completed  ${LOADTEST_REPORT}"
  else
    SUITE_LOADTEST="failed"
    bad "the load test failed"
    tail -30 "$log" | sed 's/^/        /'
  fi
}

# --------------------------------------------------------------------------- #
# The JSON report
# --------------------------------------------------------------------------- #
emit_json() {
  local ended_at duration
  ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  duration=$(( $(date +%s) - START_EPOCH ))

  local verdict="PASS"
  for suite in "$SUITE_VERIFIER" "$SUITE_BACKEND" "$SUITE_FRONTEND" \
               "$SUITE_OBSERVABILITY" "$SUITE_LOADTEST"; do
    [[ "$suite" == "failed" ]] && verdict="FAIL"
  done

  cat <<JSON
{
  "schema_version": 1,
  "verdict": "$verdict",
  "started_at": "$STARTED_AT",
  "ended_at": "$ended_at",
  "duration_seconds": $duration,
  "suites": {
    "verifier": {
      "status": "$SUITE_VERIFIER",
      "summary": "${VERIFIER_SUMMARY}",
      "log": "${VERIFIER_OUTPUT}"
    },
    "backend_tests": {
      "status": "$SUITE_BACKEND",
      "summary": "${BACKEND_SUMMARY}",
      "log": "/tmp/verify-all-backend.log"
    },
    "frontend_tests": {
      "status": "$SUITE_FRONTEND",
      "summary": "${FRONTEND_SUMMARY}",
      "log": "/tmp/verify-all-frontend.log"
    },
    "observability": {
      "status": "$SUITE_OBSERVABILITY",
      "summary": "${OBSERVABILITY_DETAIL}",
      "log": "/tmp/verify-all-observability.log"
    },
    "load_test": {
      "status": "$SUITE_LOADTEST",
      "report": "${LOADTEST_REPORT}",
      "log": "/tmp/verify-all-loadtest.log"
    }
  }
}
JSON
}

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
say "${BOLD}verify-all -- the whole platform, one command${RESET}"
note "started $STARTED_AT"

if ! ensure_stack; then
  say ""
  bad "the stack did not come up; the suites cannot run"
  SUITE_VERIFIER="failed"
  emit_json
  exit 1
fi

run_verifier
run_backend
run_frontend
run_observability
run_loadtest

echo
emit_json | tee /tmp/verify-all.json

if [[ -n "$JSON_OUT" ]]; then
  cp /tmp/verify-all.json "$JSON_OUT"
  say ""
  note "the JSON report was written to $JSON_OUT"
fi

if grep -q '"verdict": "PASS"' /tmp/verify-all.json; then
  echo
  ok "everything passed"
  exit 0
fi

echo
bad "at least one suite failed"
exit 1
