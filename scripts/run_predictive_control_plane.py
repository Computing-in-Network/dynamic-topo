#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from push_sim_policy import _write_and_reload_policy
from push_static_routes import apply_routes_incremental
from apply_predictive_route_plan import (
    _desired_routes_from_slot,
    _entries_from_plan,
    _extract_existing_managed_routes,
)
from apply_predictive_sim_policy_plan import _resolve_sim_container
from apply_predictive_neighbor_plan import (
    _desired_neighbors_from_slot,
    _entries_from_plan as _neighbor_entries_from_plan,
    _extract_existing_managed_neighbors,
    apply_neighbors_incremental,
)


ROUTE_SCHEMA = "dynamic_topo.predictive_route_plan.v1"
SIM_SCHEMA = "dynamic_topo.predictive_sim_policy_plan.v1"
NEIGHBOR_SCHEMA = "dynamic_topo.predictive_neighbor_plan.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the predictive route plan and predictive sim policy plan as a timed control-plane loop."
    )
    parser.add_argument("--route-plan", required=True, help="Predictive route plan JSON path")
    parser.add_argument("--sim-plan", required=True, help="Predictive sim policy plan JSON path")
    parser.add_argument("--neighbor-plan", default="", help="Optional predictive neighbor plan JSON path")
    parser.add_argument("--initial-offset-s", type=float, default=0.0, help="Initial relative offset into both plans")
    parser.add_argument("--poll-interval-s", type=float, default=1.0, help="Slot poll interval")
    parser.add_argument("--route-dev", default="", help="Optional route device name")
    parser.add_argument("--route-workers", type=int, default=8, help="Parallel container workers")
    parser.add_argument("--neighbor-dev", default="veth_0", help="Interface device used for permanent neighbors")
    parser.add_argument("--neighbor-workers", type=int, default=8, help="Parallel container workers for neighbors")
    parser.add_argument("--command-timeout-s", type=float, default=30.0, help="Timeout for each docker command")
    parser.add_argument(
        "--sim-container",
        default="auto",
        help="Simulator container name/id, or auto to use the sim plan hint",
    )
    parser.add_argument("--mapping-csv", default="", help="Optional mapping CSV for auto sim-container resolution")
    parser.add_argument("--sim-policy-path", default="", help="Override simulator policy path")
    parser.add_argument("--sim-proc-pattern", default="python3 /opt/sim/l2_center_sim.py")
    parser.add_argument("--route-state-output", default="", help="Optional JSON path for latest route apply state")
    parser.add_argument("--sim-state-output", default="", help="Optional JSON path for latest sim policy apply state")
    parser.add_argument("--neighbor-state-output", default="", help="Optional JSON path for latest neighbor apply state")
    parser.add_argument("--state-output", default="", help="Optional JSON path for controller state")
    parser.add_argument("--stop-at-plan-end", action="store_true", help="Exit after both plans are out of range")
    parser.add_argument("--once", action="store_true", help="Apply the slots covering the initial offset once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned changes, do not write state")
    return parser.parse_args()


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_plan(path: Path, schema: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"plan root must be a JSON object: {path}")
    if payload.get("schema") != schema:
        raise ValueError(f"unsupported plan schema for {path}: {payload.get('schema')}")
    return payload


def _select_slot(plan: dict[str, Any], offset_s: float) -> dict[str, Any] | None:
    slots = plan.get("slots")
    if not isinstance(slots, list) or not slots:
        return None
    previous: dict[str, Any] | None = None
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        start_s = float(slot.get("start_offset_s", -1.0))
        if start_s <= offset_s:
            previous = slot
            continue
        break
    if previous is None:
        return None
    if offset_s > float(plan.get("horizon_s", offset_s)):
        return None
    return previous


def _write_state(path: str, payload: dict[str, Any]) -> None:
    if not str(path).strip():
        return
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _apply_route_slot(
    *,
    plan_path: Path,
    plan: dict[str, Any],
    slot: dict[str, Any],
    route_dev: str,
    route_workers: int,
    timeout_s: float,
    dry_run: bool,
    state_output: str,
) -> tuple[bool, dict[str, Any]]:
    entries = _entries_from_plan(plan)
    desired = _desired_routes_from_slot(slot)
    applied_by_container = {
        entry.container_exec: _extract_existing_managed_routes(entry.container_exec, timeout_s)
        for entry in entries
    }
    next_state, results = apply_routes_incremental(
        desired_by_node=desired,
        entries=entries,
        applied_by_container=applied_by_container,
        route_dev=route_dev,
        dry_run=dry_run,
        timeout_s=timeout_s,
        workers=route_workers,
    )
    ok = sum(1 for item in results if item.ok)
    bad = len(results) - ok
    upserts = sum(item.upserts for item in results)
    deletes = sum(item.deletes for item in results)
    payload = {
        "schema": "dynamic_topo.predictive_route_apply_state.v1",
        "updated_at": _iso_utc_now(),
        "plan_path": str(plan_path),
        "slot_index": slot.get("slot_index"),
        "slot_start_offset_s": slot.get("start_offset_s"),
        "slot_end_offset_s": slot.get("end_offset_s"),
        "apply_ok": ok,
        "apply_fail": bad,
        "route_upserts": upserts,
        "route_deletes": deletes,
        "dry_run": dry_run,
        "applied_state": next_state if bad == 0 else {},
    }
    _write_state(state_output, payload)
    print(
        f"route_slot={slot.get('slot_index')} apply_ok={ok} apply_fail={bad} "
        f"route_upserts={upserts} route_deletes={deletes}"
    )
    for item in results:
        if not item.ok:
            print(f"[route-error] container={item.container} msg={item.error}")
    return bad == 0, payload


def _apply_sim_slot(
    *,
    plan_path: Path,
    plan: dict[str, Any],
    slot: dict[str, Any],
    sim_container: str,
    sim_policy_path: str,
    sim_proc_pattern: str,
    timeout_s: float,
    dry_run: bool,
    state_output: str,
) -> tuple[bool, dict[str, Any]]:
    policy = slot.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("predictive sim policy slot missing policy")
    apply_args = SimpleNamespace(
        sim_container=sim_container,
        sim_policy_path=sim_policy_path,
        sim_proc_pattern=sim_proc_pattern,
        output_policy="",
        command_timeout_s=timeout_s,
        dry_run=dry_run,
    )
    res = _write_and_reload_policy(apply_args, policy)
    payload = {
        "schema": "dynamic_topo.predictive_sim_policy_apply_state.v1",
        "updated_at": _iso_utc_now(),
        "plan_path": str(plan_path),
        "slot_index": slot.get("slot_index"),
        "slot_start_offset_s": slot.get("start_offset_s"),
        "slot_end_offset_s": slot.get("end_offset_s"),
        "sim_container": sim_container,
        "sim_policy_path": sim_policy_path,
        "rule_count": res.rule_count,
        "apply_ok": bool(res.ok),
        "dry_run": dry_run,
        "error": res.error,
    }
    _write_state(state_output, payload)
    status = "ok" if res.ok else "fail"
    print(f"sim_slot={slot.get('slot_index')} rules={res.rule_count} policy_apply={status}")
    if not res.ok:
        print(f"[sim-error] {res.error}")
    return bool(res.ok), payload


def _apply_neighbor_slot(
    *,
    plan_path: Path,
    plan: dict[str, Any],
    slot: dict[str, Any],
    neighbor_dev: str,
    neighbor_workers: int,
    timeout_s: float,
    dry_run: bool,
    state_output: str,
) -> tuple[bool, dict[str, Any]]:
    entries = _neighbor_entries_from_plan(plan)
    desired = _desired_neighbors_from_slot(slot)
    managed_ip_set = {entry.container_ip for entry in entries}
    applied_by_container = {
        entry.container_exec: _extract_existing_managed_neighbors(
            entry.container_exec,
            neighbor_dev=neighbor_dev,
            timeout_s=timeout_s,
            managed_ip_set=managed_ip_set,
        )
        for entry in entries
    }
    next_state, results = apply_neighbors_incremental(
        desired_by_node=desired,
        entries=entries,
        applied_by_container=applied_by_container,
        neighbor_dev=neighbor_dev,
        dry_run=dry_run,
        timeout_s=timeout_s,
        workers=neighbor_workers,
    )
    ok = sum(1 for item in results if item.ok)
    bad = len(results) - ok
    upserts = sum(item.upserts for item in results)
    deletes = sum(item.deletes for item in results)
    payload = {
        "schema": "dynamic_topo.predictive_neighbor_apply_state.v1",
        "updated_at": _iso_utc_now(),
        "plan_path": str(plan_path),
        "slot_index": slot.get("slot_index"),
        "slot_start_offset_s": slot.get("start_offset_s"),
        "slot_end_offset_s": slot.get("end_offset_s"),
        "apply_ok": ok,
        "apply_fail": bad,
        "neighbor_upserts": upserts,
        "neighbor_deletes": deletes,
        "dry_run": dry_run,
        "applied_state": next_state if bad == 0 else {},
    }
    _write_state(state_output, payload)
    print(
        f"neighbor_slot={slot.get('slot_index')} apply_ok={ok} apply_fail={bad} "
        f"neighbor_upserts={upserts} neighbor_deletes={deletes}"
    )
    for item in results:
        if not item.ok:
            print(f"[neighbor-error] container={item.container} msg={item.error}")
    return bad == 0, payload


def main() -> int:
    args = parse_args()
    route_plan_path = Path(args.route_plan).expanduser()
    sim_plan_path = Path(args.sim_plan).expanduser()
    route_plan = _load_plan(route_plan_path, ROUTE_SCHEMA)
    sim_plan = _load_plan(sim_plan_path, SIM_SCHEMA)
    neighbor_plan_path = None
    neighbor_plan = None
    if str(args.neighbor_plan).strip():
        neighbor_plan_path = Path(args.neighbor_plan).expanduser()
        neighbor_plan = _load_plan(neighbor_plan_path, NEIGHBOR_SCHEMA)

    resolved_sim_container = _resolve_sim_container(
        sim_plan,
        SimpleNamespace(
            sim_container=str(args.sim_container),
            mapping_csv=str(args.mapping_csv),
            command_timeout_s=float(args.command_timeout_s),
        ),
    )
    resolved_sim_policy_path = str(args.sim_policy_path or sim_plan.get("sim_policy_path") or "/opt/sim/policy.json")

    print(
        f"loaded route_slots={route_plan.get('slot_count')} sim_slots={sim_plan.get('slot_count')} "
        f"neighbor_slots={neighbor_plan.get('slot_count') if neighbor_plan else 0} "
        f"initial_offset_s={float(args.initial_offset_s)} dry_run={bool(args.dry_run)}"
    )

    start_mono = time.monotonic()
    last_route_slot = None
    last_sim_slot = None
    last_neighbor_slot = None
    last_route_ok = True
    last_sim_ok = True
    last_neighbor_ok = True

    while True:
        offset_s = float(args.initial_offset_s) + (time.monotonic() - start_mono)
        route_slot = _select_slot(route_plan, offset_s)
        sim_slot = _select_slot(sim_plan, offset_s)
        neighbor_slot = _select_slot(neighbor_plan, offset_s) if neighbor_plan is not None else None

        if route_slot is None and sim_slot is None and neighbor_slot is None and bool(args.stop_at_plan_end):
            print(f"plan_end_reached offset_s={offset_s:.1f}")
            return 0 if (last_route_ok and last_sim_ok and last_neighbor_ok) else 2

        if route_slot is not None:
            slot_idx = route_slot.get("slot_index")
            if slot_idx != last_route_slot:
                last_route_ok, route_state = _apply_route_slot(
                    plan_path=route_plan_path,
                    plan=route_plan,
                    slot=route_slot,
                    route_dev=str(args.route_dev),
                    route_workers=int(args.route_workers),
                    timeout_s=float(args.command_timeout_s),
                    dry_run=bool(args.dry_run),
                    state_output=str(args.route_state_output),
                )
                last_route_slot = slot_idx
            else:
                route_state = None
        else:
            route_state = None

        if sim_slot is not None:
            slot_idx = sim_slot.get("slot_index")
            if slot_idx != last_sim_slot:
                last_sim_ok, sim_state = _apply_sim_slot(
                    plan_path=sim_plan_path,
                    plan=sim_plan,
                    slot=sim_slot,
                    sim_container=resolved_sim_container,
                    sim_policy_path=resolved_sim_policy_path,
                    sim_proc_pattern=str(args.sim_proc_pattern),
                    timeout_s=float(args.command_timeout_s),
                    dry_run=bool(args.dry_run),
                    state_output=str(args.sim_state_output),
                )
                last_sim_slot = slot_idx
            else:
                sim_state = None
        else:
            sim_state = None

        if neighbor_slot is not None and neighbor_plan is not None and neighbor_plan_path is not None:
            slot_idx = neighbor_slot.get("slot_index")
            if slot_idx != last_neighbor_slot:
                last_neighbor_ok, neighbor_state = _apply_neighbor_slot(
                    plan_path=neighbor_plan_path,
                    plan=neighbor_plan,
                    slot=neighbor_slot,
                    neighbor_dev=str(args.neighbor_dev),
                    neighbor_workers=int(args.neighbor_workers),
                    timeout_s=float(args.command_timeout_s),
                    dry_run=bool(args.dry_run),
                    state_output=str(args.neighbor_state_output),
                )
                last_neighbor_slot = slot_idx
            else:
                neighbor_state = None
        else:
            neighbor_state = None

        _write_state(
            str(args.state_output),
            {
                "schema": "dynamic_topo.predictive_control_plane_state.v1",
                "updated_at": _iso_utc_now(),
                "offset_s": offset_s,
                "route_plan": str(route_plan_path),
                "sim_plan": str(sim_plan_path),
                "neighbor_plan": str(neighbor_plan_path) if neighbor_plan_path is not None else "",
                "route_slot_index": last_route_slot,
                "sim_slot_index": last_sim_slot,
                "neighbor_slot_index": last_neighbor_slot,
                "route_ok": last_route_ok,
                "sim_ok": last_sim_ok,
                "neighbor_ok": last_neighbor_ok,
                "sim_container": resolved_sim_container,
                "sim_policy_path": resolved_sim_policy_path,
                "dry_run": bool(args.dry_run),
            },
        )

        if bool(args.once):
            return 0 if (last_route_ok and last_sim_ok and last_neighbor_ok) else 2

        sleep_s = max(0.1, float(args.poll_interval_s))
        time.sleep(sleep_s)


if __name__ == "__main__":
    raise SystemExit(main())
