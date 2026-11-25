#!/usr/bin/env bash
#
# demo-normal.sh -- the "normal operations" half of the demo.
#
# This is beats 1-8 of the 14-beat narrative in `docs/DEMO.md`: the platform
# working, with no fault injected. It prints the exact command for each
# beat and the observable output it should produce, so a judge can follow
# along on the console and see the same thing the script sees.
#
# The script is *not* a test -- it does not assert, it narrates. But it does
# fail loudly if the stack is not up, because a demo that silently runs
# against a dead backend is worse than no demo.
#
# Usage:
#   ./scripts/demo-normal.sh              # narrate + run the commands
#   ./scripts/demo-normal.sh --narrate    # print the commands only

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

SIM="${SIMULATOR_URL:-http://localhost:8000}"
BACKEND="${BACKEND_URL:-http://localhost:8002}"
CONSOLE="${CONSOLE_URL:-http://localhost:8003}"

NARRATE_ONLY=0
[[ "${1:-}" == "--narrate" ]] && NARRATE_ONLY=1

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
  CYAN=$'\033[36m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; CYAN=""; DIM=""; RESET=""
fi

beat() {
  echo ""
  printf '%s%s%s\n' "$BOLD" "$1" "$RESET"
  echo "${DIM}--------------------------------------------------------------$RESET"
}

cmd() {
  echo ""
  printf '%s$ %s%s\n' "$CYAN" "$*" "$RESET"
}

note() { printf '%s%s%s\n' "$DIM" "$*" "$RESET"; }
expect() { printf '%sexpect:%s %s\n' "$YELLOW" "$RESET" "$*"; }

run() {
  if [[ $NARRATE_ONLY -eq 1 ]]; then
    return 0
  fi
  "$@" 2>&1 | head -25
}

# --------------------------------------------------------------------------- #
# Preconditions
# --------------------------------------------------------------------------- #
beat "Preconditions -- the stack must be up"
cmd "docker compose ps"
note "Expect: 5 services, all healthy (simulator-api, redis, client-verifier,"
note "        backend, frontend)."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  if ! curl -fsS -m 5 "$SIM/v1/health" >/dev/null 2>&1; then
    printf '%sThe simulator is not answering at %s. Start the stack first:%s\n' \
      "$YELLOW" "$SIM" "$RESET"
    echo "  docker compose up -d --build"
    exit 1
  fi
  if ! curl -fsS -m 5 "$BACKEND/api/health" >/dev/null 2>&1; then
    printf '%sThe backend is not answering at %s. Start the stack first:%s\n' \
      "$YELLOW" "$BACKEND" "$RESET"
    echo "  docker compose up -d --build"
    exit 1
  fi
  printf '%sThe stack is up.%s\n' "$GREEN" "$RESET"
fi

# --------------------------------------------------------------------------- #
beat "Beat 1 -- Normal operations"
cmd "curl -s $BACKEND/api/state"
expect "The world snapshot: a tick number, the stations, their inventory."
note "On the console: the header shows the live tick and the world looks healthy."

beat "Beat 2 -- The operator dashboard"
cmd "open $CONSOLE"
expect "The console: the live world, the problem list, the recommendations."
note "Every number on screen came from the backend -- the no-invented-numbers"
note "test enforces that, so nothing here is a guess."

beat "Beat 3 -- Demand is increasing"
cmd "curl -s $BACKEND/api/forecast/summary"
expect "The forecast summary: the projected demand per station and fuel."
note "The forecast is computed offline from the simulator's own seeded RNG,"
note "so it matches the future exactly -- not approximately."

beat "Beat 4 -- A risk is detected"
cmd "curl -s $BACKEND/api/anomalies/summary"
expect "The anomaly summary. Empty is the right answer here -- nothing is wrong."
note "In beat 9 the crisis script injects a disruption and this becomes non-empty."

beat "Beat 5 -- The prediction"
cmd "curl -s $BACKEND/api/forecast | head -c 600"
expect "The forecast series with each station's projected stockout risk."
note "The forecast tab shows the series, the stockout risk and the analytic"
note "probability of running dry."

beat "Beat 6 -- The recommendation"
cmd "curl -s $BACKEND/api/optimize/solver"
expect "Which solver is active (greedy or lp) and why."
note "The ActionCard on the console shows the recommendation: one shipment,"
note "one reason, one number -- the reason being why the system chose it."

beat "Beat 7 -- The operator inspects"
cmd "curl -s $BACKEND/api/anomalies | head -c 400"
expect "The anomalies, each with an explain payload behind it."
note "The Details disclosure in the console shows the risks, the next actions"
note "and the gate's reasoning -- the gate is the component that validates a"
note "plan before it reaches the simulator, so an approved plan is never"
note "rejected with a 409."

beat "Beat 8 -- The allocation is simulated"
cmd "curl -s -X POST $BACKEND/api/plan -H 'Content-Type: application/json' \\"
cmd "  -d '{\"source_depot_id\":\"depot-gazipur\",\"destination_station_id\":\"station-mirpur\",\"fuel_type\":\"DIESEL\",\"quantity\":500}'"
expect "A plan: the shipments that would be dispatched. Nothing is written yet."
note "POST /api/plan is a dry run -- it plans, it does not dispatch. The console"
note "then makes the operator click twice to actually dispatch, so a shipment"
note "never leaves without a human saying so."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  run curl -s -X POST "$BACKEND/api/plan" -H 'Content-Type: application/json' \
    -d '{"source_depot_id":"depot-gazipur","destination_station_id":"station-mirpur","fuel_type":"DIESEL","quantity":500}'
fi

beat "Beat 8b -- The dispatch journal"
cmd "curl -s $BACKEND/api/health | head -c 400"
expect "The health payload, including the journal and the circuit state."
note "Every dispatch intent is journalled before it is sent, so a crash between"
note "'recorded' and 'sent' is replayed on the next boot -- and the simulator's"
note "idempotency means the replay produces the original shipment, not a"
note "duplicate."

# --------------------------------------------------------------------------- #
echo ""
printf '%s%s%s\n' "$BOLD" "The normal-operations half is done." "$RESET"
note "The crisis, fault and recovery beats are in:"
note "  ./scripts/demo-crisis-fault-recovery.sh"
note "And the whole story in one command:"
note "  ./scripts/verify-all.sh"
