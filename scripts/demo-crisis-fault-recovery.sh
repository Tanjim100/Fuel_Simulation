#!/usr/bin/env bash
#
# demo-crisis-fault-recovery.sh -- the resilience half of the demo.
#
# This is beats 9-14 of the 14-beat narrative in `docs/DEMO.md`: a crisis
# event, the system's response, an injected upstream failure, the
# observability that detects it, the degraded mode that keeps the console
# alive, and the recovery that needs no operator.
#
# PDF section 20 item 9 asks for a "resilience demonstration" and section 12
# says "clear and demonstrable behavior is more important" than sophisticated
# infrastructure. This script IS the demonstration: it injects the fault,
# shows the breaker opening, shows the cached world still being served with
# an honest `degraded` flag, shows the write path refusing rather than
# writing against a stale world, and then shows the system recovering on its
# own.
#
# Usage:
#   ./scripts/demo-crisis-fault-recovery.sh              # run it
#   ./scripts/demo-crisis-fault-recovery.sh --narrate    # print only

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

SIM="${SIMULATOR_URL:-http://localhost:8000}"
BACKEND="${BACKEND_URL:-http://localhost:8002}"
CONSOLE="${CONSOLE_URL:-http://localhost:8003}"

#: How long the injected outage lasts. Long enough for the breaker to trip
#: (5 consecutive failures) and for the cooldown to be visible, short enough
#: that the demo does not drag.
FAULT_SECONDS="${FAULT_SECONDS:-60}"

NARRATE_ONLY=0
[[ "${1:-}" == "--narrate" ]] && NARRATE_ONLY=1

# --------------------------------------------------------------------------- #
# The fault must not outlive the script.
#
# The injected outage lasts 60 seconds and the script may be interrupted at
# any point -- Ctrl-C during the wait for the breaker to trip is the likely
# case. Without a trap, the fault stays active and the backend stays
# degraded for the rest of the judging session, which is exactly the wrong
# state to be left in.
#
# The trap fires on normal exit too, so the explicit clear further down is
# belt-and-braces rather than the only path.
# --------------------------------------------------------------------------- #
FAULT_INJECTED=0
cleanup_fault() {
  if [[ $FAULT_INJECTED -eq 1 ]]; then
    curl -s -m 5 -X POST "$SIM/admin/faults/clear" >/dev/null 2>&1 || true
    if [[ -t 1 ]]; then
      echo ""
      printf '%sThe injected fault has been cleared.%s\n' "$DIM" "$RESET"
    fi
  fi
}
trap cleanup_fault EXIT INT TERM

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
cmd() { printf '%s$ %s%s\n' "$CYAN" "$*" "$RESET"; }
note() { printf '%s%s%s\n' "$DIM" "$*" "$RESET"; }
expect() { printf '%sexpect:%s %s\n' "$YELLOW" "$RESET" "$*"; }

# A tiny JSON field reader, so the script does not depend on jq being
# installed on the judge's machine.
jget() { python3 -c "
import json,sys
try:
    data = json.load(sys.stdin)
except Exception:
    print(''); raise SystemExit(0)
for key in sys.argv[1].split('.'):
    if isinstance(data, dict) and key in data:
        data = data[key]
    else:
        print(''); raise SystemExit(0)
print(data)
" "$1"; }

# --------------------------------------------------------------------------- #
beat "Preconditions"
cmd "docker compose ps"
note "Expect: 5 healthy services."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  if ! curl -fsS -m 5 "$SIM/v1/health" >/dev/null 2>&1; then
    printf '%sThe simulator is not answering at %s.%s\n' "$YELLOW" "$SIM" "$RESET"
    exit 1
  fi
  if ! curl -fsS -m 5 "$BACKEND/api/health" >/dev/null 2>&1; then
    printf '%sThe backend is not answering at %s.%s\n' "$YELLOW" "$BACKEND" "$RESET"
    exit 1
  fi
  printf '%sThe stack is up.%s\n' "$GREEN" "$RESET"
fi

# --------------------------------------------------------------------------- #
beat "Beat 9 -- A crisis event"
cmd "curl -s -X POST $SIM/admin/events \\"
cmd "  -H 'Content-Type: application/json' \\"
cmd "  -d '{\"type\":\"route_disruption\",\"start_tick\":0,\"duration_ticks\":6}'"
expect "HTTP 201. The route is disrupted for six ticks."
note "A route disruption on a route the planner wants to use forces the"
note "optimiser to find another way -- or to report that there is none. The"
note "interesting part is that it decides *honestly*."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  curl -s -X POST "$SIM/admin/events" -H 'Content-Type: application/json' \
    -d '{"type":"route_disruption","start_tick":0,"duration_ticks":6}' \
    | head -c 400
  echo ""
fi

# --------------------------------------------------------------------------- #
beat "Beat 10 -- The system adapts"
cmd "curl -s -X POST $BACKEND/api/plan -H 'Content-Type: application/json' \\"
cmd "  -d '{\"source_depot_id\":\"depot-gazipur\",\"destination_station_id\":\"station-mirpur\",\"fuel_type\":\"DIESEL\",\"quantity\":500}'"
expect "A plan that avoids the disrupted route, or an honest refusal."
note "The plan is the system's answer to the crisis: the optimiser rerouted,"
note "and the console shows the plan change with the reason attached."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  curl -s -X POST "$BACKEND/api/plan" -H 'Content-Type: application/json' \
    -d '{"source_depot_id":"depot-gazipur","destination_station_id":"station-mirpur","fuel_type":"DIESEL","quantity":500}' \
    | head -c 500
  echo ""
fi

beat "Beat 10b -- The anomaly appears"
cmd "curl -s $BACKEND/api/anomalies/summary"
expect "The disruption registers as an anomaly the operator can see."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  curl -s "$BACKEND/api/anomalies/summary" | head -c 400
  echo ""
fi

# --------------------------------------------------------------------------- #
beat "Beat 11 -- A fault is injected into the upstream"
cmd "curl -s -X POST $SIM/admin/faults \\"
cmd "  -H 'Content-Type: application/json' \\"
cmd "  -d '{\"type\":\"unavailable\",\"duration_seconds\":$FAULT_SECONDS}'"
expect "HTTP 201. The simulator now returns 503 to every data request."
note "This is the real test. The simulator is still running -- its /health"
note "still answers -- but its data endpoints fail. That is precisely the"
note "case a naive health check gets wrong."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  curl -s -X POST "$SIM/admin/faults" -H 'Content-Type: application/json' \
    -d "{\"type\":\"unavailable\",\"duration_seconds\":$FAULT_SECONDS}" \
    | head -c 300
  echo ""
  FAULT_INJECTED=1
fi

# --------------------------------------------------------------------------- #
beat "Beat 12 -- The monitoring detects it"
note "Waiting for the breaker to trip (5 consecutive retryable failures)..."
note "This takes a few seconds -- the client retries with backoff first."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  for _ in $(seq 1 30); do
    state=$(curl -s -m 5 "$BACKEND/api/health" | jget circuit.state)
    if [[ "$state" == "OPEN" ]]; then
      break
    fi
    sleep 2
  done
fi

cmd "curl -s $BACKEND/api/health"
expect "status: degraded. The circuit is OPEN."
note "The circuit state is the *data path's* truth, not the simulator's. The"
note "simulator's /health still answers -- the fault only kills the data"
note "endpoints -- so a probe-only check would have called this system healthy."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  curl -s "$BACKEND/api/health" | head -c 700
  echo ""
fi

cmd "curl -s $BACKEND/metrics | grep backend_circuit_state"
expect "backend_circuit_state 2.0  (0=CLOSED, 1=HALF_OPEN, 2=OPEN)"

if [[ $NARRATE_ONLY -eq 0 ]]; then
  curl -s "$BACKEND/metrics" | grep -E 'backend_circuit_state|fallback_activations|upstream_requests_total' | head -10
fi

# --------------------------------------------------------------------------- #
beat "Beat 13 -- The degraded mode keeps the console alive"
cmd "curl -s $BACKEND/api/state"
expect "HTTP 200, with \"degraded\": true and a degraded_reason."
note "The console keeps working. It shows the last known good world and it"
note "SAYS that the data is degraded -- a stale world presented as live would"
note "be far worse than a visible degradation."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  # The `degraded` flag is what matters, and it sits at the *end* of the
  # payload -- so read the field rather than truncating the body.
  curl -s "$BACKEND/api/state" | python3 -c "
import json,sys
payload = json.load(sys.stdin)
print('degraded      :', payload.get('degraded'))
print('degraded_reason:', payload.get('degraded_reason'))
print('tick          :', payload.get('state', {}).get('tick'))
print('stations      :', len(payload.get('state', {}).get('stations', [])))
"
fi

cmd "curl -s -X POST $BACKEND/api/plan -H 'Content-Type: application/json' \\"
cmd "  -d '{\"source_depot_id\":\"depot-gazipur\",\"destination_station_id\":\"station-mirpur\",\"fuel_type\":\"DIESEL\",\"quantity\":500}'"
expect "HTTP 503 with code CIRCUIT_OPEN."
note "But the WRITE path refuses. Planning against a world that cannot be read"
note "would produce a plan based on stale inventory -- and the gate exists"
note "precisely to stop a bad shipment. A read may be served stale (and say"
note "so); a write may not."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BACKEND/api/plan" \
    -H 'Content-Type: application/json' \
    -d '{"source_depot_id":"depot-gazipur","destination_station_id":"station-mirpur","fuel_type":"DIESEL","quantity":500}'
  curl -s -X POST "$BACKEND/api/plan" -H 'Content-Type: application/json' \
    -d '{"source_depot_id":"depot-gazipur","destination_station_id":"station-mirpur","fuel_type":"DIESEL","quantity":500}' \
    | head -c 400
  echo ""
fi

# --------------------------------------------------------------------------- #
beat "Beat 14 -- Recovery"
cmd "curl -s -X POST $SIM/admin/faults/clear"
expect "HTTP 200. The fault is cleared."
note "Now nothing else happens. The breaker's cooldown elapses, its HALF_OPEN"
note "probe succeeds, and the breaker closes by itself. No operator action,"
note "no restart, no manual cache invalidation."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  curl -s -X POST "$SIM/admin/faults/clear" | head -c 200
  echo ""
fi

note "Waiting for the breaker to close (the cooldown is 15s by default)..."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  for _ in $(seq 1 45); do
    state=$(curl -s -m 5 "$BACKEND/api/health" | jget circuit.state)
    if [[ "$state" == "CLOSED" ]]; then
      break
    fi
    sleep 2
  done
fi

cmd "curl -s $BACKEND/api/health"
expect "status: ok. The circuit is CLOSED."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  curl -s "$BACKEND/api/health" | head -c 500
  echo ""
fi

cmd "curl -s $BACKEND/api/state"
expect "No \"degraded\" key -- the world is live again."

if [[ $NARRATE_ONLY -eq 0 ]]; then
  degraded=$(curl -s "$BACKEND/api/state" | jget degraded)
  if [[ -z "$degraded" ]]; then
    printf '%sThe world is live again -- no degraded flag.%s\n' "$GREEN" "$RESET"
  else
    printf '%sStill degraded -- check the breaker state.%s\n' "$YELLOW" "$RESET"
  fi
fi

# --------------------------------------------------------------------------- #
echo ""
printf '%s%s%s\n' "$BOLD" "The resilience half is done." "$RESET"
note "The normal-operations half is in:"
note "  ./scripts/demo-normal.sh"
note "And the whole story in one command:"
note "  ./scripts/verify-all.sh"
