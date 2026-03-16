#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/run/star10_dv_route_a}"
SUBSET_MAPPING_CSV="${SUBSET_MAPPING_CSV:-$RUN_DIR/node_mapping_10.csv}"
NODE_COUNT="${NODE_COUNT:-10}"

stop_one() {
  local name="$1"
  local pid_file="$RUN_DIR/${name}.pid"
  if [[ ! -f "$pid_file" ]]; then
    return 0
  fi
  local pid
  pid="$(cat "$pid_file")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
  echo "stopped $name"
}

stop_one "push_dv_neighbor_state"
stop_one "push_sim_policy"
stop_one "stream_server"

if [[ -f "$SUBSET_MAPPING_CSV" ]]; then
  "$PYTHON_BIN" scripts/manage_dv_agents.py \
    --action stop \
    --mapping-csv "$SUBSET_MAPPING_CSV" \
    --max-nodes "$NODE_COUNT"
else
  echo "skip agent stop: subset mapping not found at $SUBSET_MAPPING_CSV"
fi
