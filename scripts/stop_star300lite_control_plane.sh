#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/run/star300lite}"

stop_one() {
  local name="$1"
  local pid_file="$RUN_DIR/${name}.pid"
  if [[ ! -f "$pid_file" ]]; then
    echo "$name not-managed"
    return 0
  fi
  local pid
  pid="$(cat "$pid_file")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    echo "$name stopped pid=$pid"
  else
    echo "$name stale-pid=$pid"
  fi
  rm -f "$pid_file"
}

stop_one "push_static_routes"
stop_one "push_sim_policy"
stop_one "stream_server"
