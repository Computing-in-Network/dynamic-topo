#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
WS_URL="${WS_URL:-ws://127.0.0.1:8765}"
CONTAINER_PREFIX="${CONTAINER_PREFIX:-star300lite_r_}"
SIM_CONTAINER="${SIM_CONTAINER:-star300lite_sim}"
MAPPING_CSV="${MAPPING_CSV:-docs/node_mapping_300.csv}"
MIN_STABLE_FRAMES="${MIN_STABLE_FRAMES:-2}"
POLICY_APPLY_INTERVAL_S="${POLICY_APPLY_INTERVAL_S:-30}"
ROUTE_APPLY_INTERVAL_S="${ROUTE_APPLY_INTERVAL_S:-30}"
START_STREAM_SERVER="${START_STREAM_SERVER:-0}"
STREAM_HOST="${STREAM_HOST:-0.0.0.0}"
STREAM_PORT="${STREAM_PORT:-8765}"
STREAM_DT="${STREAM_DT:-1.0}"

RUN_DIR="${RUN_DIR:-$ROOT_DIR/run/star300lite}"
ROUTE_SNAPSHOT_PATH="${ROUTE_SNAPSHOT_PATH:-$RUN_DIR/route_snapshot.json}"
mkdir -p "$RUN_DIR"

start_bg() {
  local name="$1"
  shift
  local log_file="$RUN_DIR/${name}.log"
  local pid_file="$RUN_DIR/${name}.pid"
  setsid bash -lc 'echo $$ > "$1"; shift; exec "$@"' _ "$pid_file" "$@" \
    </dev/null >"$log_file" 2>&1 &
  local pid=""
  for _ in 1 2 3 4 5; do
    if [[ -s "$pid_file" ]]; then
      pid="$(cat "$pid_file")"
      break
    fi
    sleep 1
  done
  if [[ -z "$pid" ]]; then
    echo "[fatal] failed to capture pid for $name, see $log_file" >&2
    exit 1
  fi
  echo "$pid" >"$pid_file"
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[fatal] failed to start $name, see $log_file" >&2
    exit 1
  fi
  echo "$name pid=$pid log=$log_file"
}

stop_managed() {
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
}

stop_managed "push_sim_policy"
stop_managed "push_static_routes"
if [[ "$START_STREAM_SERVER" == "1" ]]; then
  stop_managed "stream_server"
fi

"$PYTHON_BIN" scripts/generate_node_mapping.py \
  --container-prefix "$CONTAINER_PREFIX" \
  --output "$MAPPING_CSV"

"$PYTHON_BIN" scripts/restore_sim_datapath.py \
  --sim-container "$SIM_CONTAINER" \
  --check-ovs \
  --restart-sim

if [[ "$START_STREAM_SERVER" == "1" ]]; then
  start_bg "stream_server" \
    "$PYTHON_BIN" -u -m dynamic_topo.stream_server \
    --host "$STREAM_HOST" \
    --port "$STREAM_PORT" \
    --dt "$STREAM_DT" \
    --route-snapshot "$ROUTE_SNAPSHOT_PATH"
fi

start_bg "push_sim_policy" \
  "$PYTHON_BIN" -u scripts/push_sim_policy.py \
  --ws-url "$WS_URL" \
  --mapping-csv "$MAPPING_CSV" \
  --sim-container auto \
  --sim-policy-path /opt/sim/policy.json \
  --min-stable-frames "$MIN_STABLE_FRAMES" \
  --min-apply-interval-s "$POLICY_APPLY_INTERVAL_S"

start_bg "push_static_routes" \
  "$PYTHON_BIN" -u scripts/push_static_routes.py \
  --ws-url "$WS_URL" \
  --mapping-csv "$MAPPING_CSV" \
  --container-ip-source docker \
  --min-stable-frames "$MIN_STABLE_FRAMES" \
  --min-apply-interval-s "$ROUTE_APPLY_INTERVAL_S" \
  --state-output "$ROUTE_SNAPSHOT_PATH"

echo "run_dir=$RUN_DIR"
