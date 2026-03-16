#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export NODE_COUNT="${NODE_COUNT:-100}"
export SUBSET_STRATEGY="${SUBSET_STRATEGY:-first-n}"
export RUN_DIR="${RUN_DIR:-$ROOT_DIR/run/star100_dv_route_a}"
export SUBSET_MAPPING_CSV="${SUBSET_MAPPING_CSV:-$RUN_DIR/node_mapping_100.csv}"
export DV_ROUTE_TIMEOUT_S="${DV_ROUTE_TIMEOUT_S:-60}"

exec "$ROOT_DIR/scripts/start_star10_dv_route_a.sh"
