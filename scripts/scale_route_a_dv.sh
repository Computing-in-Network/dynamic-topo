#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
COUNTS="${COUNTS:-110 120 130 140 145 150 160 170 180 190 200 225 250 275 300}"
SUBSET_STRATEGY="${SUBSET_STRATEGY:-largest-component-snapshot}"
POLL_INTERVAL_S="${POLL_INTERVAL_S:-10}"
RESULTS_FILE="${RESULTS_FILE:-$ROOT_DIR/run/route_a_dv_scale/results.tsv}"
TOPOLOGY_SAMPLE_FRAMES="${TOPOLOGY_SAMPLE_FRAMES:-20}"

mkdir -p "$(dirname "$RESULTS_FILE")"
printf "count\troute_timeout_s\tagents\tnode1_routes\tnodeN_routes\tping\tresult\n" >"$RESULTS_FILE"

calc_timeout() {
  local count="$1"
  if (( count <= 100 )); then
    echo 60
  elif (( count <= 150 )); then
    echo 90
  elif (( count <= 200 )); then
    echo 120
  elif (( count <= 250 )); then
    echo 150
  else
    echo 180
  fi
}

calc_max_wait() {
  local count="$1"
  if (( count <= 150 )); then
    echo 120
  elif (( count <= 200 )); then
    echo 180
  elif (( count <= 250 )); then
    echo 240
  else
    echo 300
  fi
}

subset_target_info() {
  local subset_csv="$1"
  "$PYTHON_BIN" - <<'PY' "$subset_csv"
import csv
import ipaddress
import sys

with open(sys.argv[1], "r", encoding="utf-8", newline="") as fp:
    rows = list(csv.DictReader(fp))
if not rows:
    raise SystemExit("empty subset csv")
rows.sort(key=lambda row: int(row["node_index"]))
src = rows[0]
dst = rows[-1]
idx = int(dst["node_index"])
if idx <= 254:
    dst_ip = f"10.255.0.{idx}"
else:
    dst_ip = f"10.255.1.{idx - 254}"
print(src["container_name"])
print(dst["container_name"])
print(dst_ip)
print(src["node_id"])
print(dst["node_id"])
PY
}

stop_scale() {
  local count="$1"
  local run_dir="$ROOT_DIR/run/star${count}_dv_route_a"
  local subset_csv="$run_dir/node_mapping_${count}.csv"
  RUN_DIR="$run_dir" SUBSET_MAPPING_CSV="$subset_csv" NODE_COUNT="$count" \
    "$ROOT_DIR/scripts/stop_star10_dv_route_a.sh" >/dev/null 2>&1 || true
}

snapshot_best_component() {
  local count="$1"
  local run_dir="$ROOT_DIR/run/star${count}_dv_route_a"
  local snap="$run_dir/topology_component_snapshot.json"
  if [[ ! -f "$snap" ]]; then
    echo ""
    return 0
  fi
  "$PYTHON_BIN" - <<'PY' "$snap"
import json
import sys
from pathlib import Path
p = Path(sys.argv[1])
data = json.loads(p.read_text(encoding="utf-8"))
print(int(data.get("best_component_size", 0)))
PY
}

wait_for_agents() {
  local count="$1"
  local subset_csv="$2"
  local waited=0
  local max_wait="$3"
  while (( waited <= max_wait )); do
    local agents
    agents="$("$PYTHON_BIN" scripts/manage_dv_agents.py --action status --mapping-csv "$subset_csv" --max-nodes "$count" | wc -l | tr -d ' ')"
    if [[ "$agents" == "$count" ]]; then
      echo "$agents"
      return 0
    fi
    sleep "$POLL_INTERVAL_S"
    waited=$((waited + POLL_INTERVAL_S))
  done
  "$PYTHON_BIN" scripts/manage_dv_agents.py --action status --mapping-csv "$subset_csv" --max-nodes "$count" | wc -l | tr -d ' '
}

validate_scale() {
  local count="$1"
  local route_timeout_s="$2"
  local run_dir="$ROOT_DIR/run/star${count}_dv_route_a"
  local subset_csv="$run_dir/node_mapping_${count}.csv"
  local max_wait
  max_wait="$(calc_max_wait "$count")"
  mapfile -t target_info < <(subset_target_info "$subset_csv")
  local src_container="${target_info[0]}"
  local dst_container="${target_info[1]}"
  local target_ip="${target_info[2]}"
  local src_node="${target_info[3]}"
  local dst_node="${target_info[4]}"

  local agents
  agents="$(wait_for_agents "$count" "$subset_csv" "$max_wait")"
  if [[ "$agents" != "$count" ]]; then
    printf "%s\t%s\t%s\t-\t-\t-\tFAIL_agents\n" "$count" "$route_timeout_s" "$agents" >>"$RESULTS_FILE"
    echo "count=$count result=FAIL_agents agents=$agents expected=$count"
    return 1
  fi

  local waited=0
  while (( waited <= max_wait )); do
    local routes1 routesn ping_rc
    routes1="$(docker exec "$src_container" sh -lc 'ip -4 route show | grep "^10\.255" | wc -l' | tr -d ' ')"
    routesn="$(docker exec "$dst_container" sh -lc 'ip -4 route show | grep "^10\.255" | wc -l' | tr -d ' ')"
    ping_rc="$(docker exec "$src_container" sh -lc "ping -c 1 -W 1 $target_ip >/dev/null 2>&1; echo \$?" | tr -d ' ')"

    if [[ "$routes1" == "$((count - 1))" && "$routesn" == "$((count - 1))" && "$ping_rc" == "0" ]]; then
      printf "%s\t%s\t%s\t%s\t%s\t%s\tPASS\n" "$count" "$route_timeout_s" "$agents" "$routes1" "$routesn" "$ping_rc" >>"$RESULTS_FILE"
      echo "count=$count result=PASS agents=$agents src=$src_node dst=$dst_node src_routes=$routes1 dst_routes=$routesn ping=$ping_rc"
      return 0
    fi

    echo "count=$count settling waited=${waited}s agents=$agents src=$src_node dst=$dst_node src_routes=$routes1 dst_routes=$routesn ping=$ping_rc"
    sleep "$POLL_INTERVAL_S"
    waited=$((waited + POLL_INTERVAL_S))
  done

  local routes1 routesn ping_rc
  routes1="$(docker exec "$src_container" sh -lc 'ip -4 route show | grep "^10\.255" | wc -l' | tr -d ' ')"
  routesn="$(docker exec "$dst_container" sh -lc 'ip -4 route show | grep "^10\.255" | wc -l' | tr -d ' ')"
  ping_rc="$(docker exec "$src_container" sh -lc "ping -c 1 -W 1 $target_ip >/dev/null 2>&1; echo \$?" | tr -d ' ')"
  printf "%s\t%s\t%s\t%s\t%s\t%s\tFAIL_converge\n" "$count" "$route_timeout_s" "$agents" "$routes1" "$routesn" "$ping_rc" >>"$RESULTS_FILE"
  echo "count=$count result=FAIL_converge agents=$agents src=$src_node dst=$dst_node src_routes=$routes1 dst_routes=$routesn ping=$ping_rc"
  return 1
}

current_count=100

for count in $COUNTS; do
  route_timeout_s="$(calc_timeout "$count")"
  run_dir="$ROOT_DIR/run/star${count}_dv_route_a"
  subset_csv="$run_dir/node_mapping_${count}.csv"

  stop_scale "$current_count"

  echo "starting count=$count route_timeout_s=$route_timeout_s run_dir=$run_dir"
  if ! RUN_DIR="$run_dir" \
    SUBSET_MAPPING_CSV="$subset_csv" \
    NODE_COUNT="$count" \
    SUBSET_STRATEGY="$SUBSET_STRATEGY" \
    TOPOLOGY_SAMPLE_FRAMES="$TOPOLOGY_SAMPLE_FRAMES" \
    DV_ROUTE_TIMEOUT_S="$route_timeout_s" \
      "$ROOT_DIR/scripts/start_star10_dv_route_a.sh"; then
    best_component="$(snapshot_best_component "$count")"
    printf "%s\t%s\t-\t-\t-\t-\tFAIL_component_cap(%s)\n" "$count" "$route_timeout_s" "${best_component:-unknown}" >>"$RESULTS_FILE"
    echo "count=$count result=FAIL_component_cap best_component=${best_component:-unknown}"
    exit 1
  fi

  if ! validate_scale "$count" "$route_timeout_s"; then
    echo "scale-up stopped at count=$count"
    exit 1
  fi
  current_count="$count"
done

echo "scale-up completed counts=[$COUNTS]"
echo "results_file=$RESULTS_FILE"
