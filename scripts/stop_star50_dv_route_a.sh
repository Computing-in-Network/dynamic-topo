#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export NODE_COUNT="${NODE_COUNT:-50}"
export RUN_DIR="${RUN_DIR:-$ROOT_DIR/run/star50_dv_route_a}"
export SUBSET_MAPPING_CSV="${SUBSET_MAPPING_CSV:-$RUN_DIR/node_mapping_50.csv}"

exec "$ROOT_DIR/scripts/stop_star10_dv_route_a.sh"
