#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from push_static_routes import apply_routes_incremental, NodeEntry


_MANAGED_PREFIX_RE = re.compile(r"^(10\.255\.[0-9]+\.[0-9]+/[0-9]+)\s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply one slot from a predictive route plan into containers."
    )
    parser.add_argument("--plan", required=True, help="Predictive route plan JSON path")
    parser.add_argument("--slot-index", type=int, default=-1, help="Slot index to apply")
    parser.add_argument(
        "--at-offset-s",
        type=float,
        default=-1.0,
        help="Pick the slot that covers this relative offset instead of explicit slot-index",
    )
    parser.add_argument("--route-dev", default="", help="Optional route device name")
    parser.add_argument("--workers", type=int, default=8, help="Parallel container workers")
    parser.add_argument("--command-timeout-s", type=float, default=30.0, help="Timeout for each docker exec")
    parser.add_argument("--dry-run", action="store_true", help="Print commands only, do not execute")
    parser.add_argument(
        "--state-output",
        default="",
        help="Optional JSON path recording the last applied predictive plan slot",
    )
    return parser.parse_args()


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("plan root must be a JSON object")
    if payload.get("schema") != "dynamic_topo.predictive_route_plan.v1":
        raise ValueError(f"unsupported predictive route plan schema: {payload.get('schema')}")
    return payload


def _select_slot(plan: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    slots = plan.get("slots")
    if not isinstance(slots, list) or not slots:
        raise ValueError("predictive route plan has no slots")
    if int(args.slot_index) >= 0:
        idx = int(args.slot_index)
        if idx >= len(slots):
            raise ValueError(f"slot-index out of range: {idx} >= {len(slots)}")
        slot = slots[idx]
        if not isinstance(slot, dict):
            raise ValueError(f"invalid slot at index {idx}")
        return slot
    if float(args.at_offset_s) >= 0.0:
        offset_s = float(args.at_offset_s)
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            start_s = float(slot.get("start_offset_s", -1.0))
            end_s = float(slot.get("end_offset_s", -1.0))
            if start_s <= offset_s <= end_s:
                return slot
        raise ValueError(f"no predictive route slot covers at-offset-s={offset_s}")
    slot = slots[0]
    if not isinstance(slot, dict):
        raise ValueError("invalid slot at index 0")
    return slot


def _entries_from_plan(plan: dict[str, Any]) -> list[NodeEntry]:
    nodes = plan.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError("predictive route plan missing nodes payload")
    entries: list[NodeEntry] = []
    for node_id, payload in nodes.items():
        if not isinstance(payload, dict):
            continue
        entries.append(
            NodeEntry(
                node_id=str(node_id),
                node_index=int(payload["node_index"]),
                container_name=str(payload.get("container_name", "")),
                container_exec=str(payload["container_exec"]),
                container_ip=str(payload["container_ip"]),
                loopback_prefix=str(payload["loopback_prefix"]),
            )
        )
    entries.sort(key=lambda entry: entry.node_index)
    return entries


def _desired_routes_from_slot(slot: dict[str, Any]) -> dict[str, dict[str, str]]:
    desired = slot.get("desired_routes")
    if not isinstance(desired, dict):
        raise ValueError("predictive route slot missing desired_routes")
    normalized: dict[str, dict[str, str]] = {}
    for node_id, routes in desired.items():
        if not isinstance(routes, dict):
            continue
        normalized[str(node_id)] = {str(prefix): str(next_hop) for prefix, next_hop in routes.items()}
    return normalized


def _extract_existing_managed_routes(container: str, timeout_s: float) -> dict[str, str]:
    proc = subprocess.run(
        ["docker", "exec", container, "ip", "-4", "route", "show"],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"failed to read routes from {container}: {err[:300]}")
    routes: dict[str, str] = {}
    for raw in proc.stdout.splitlines():
        m = _MANAGED_PREFIX_RE.match(raw.strip())
        if not m:
            continue
        prefix = m.group(1)
        parts = raw.split()
        if "via" not in parts:
            continue
        via_idx = parts.index("via")
        if via_idx + 1 >= len(parts):
            continue
        routes[prefix] = parts[via_idx + 1]
    return routes


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    plan_path = Path(args.plan).expanduser()
    plan = _load_plan(plan_path)
    slot = _select_slot(plan, args)
    entries = _entries_from_plan(plan)
    desired = _desired_routes_from_slot(slot)

    applied_by_container = {
        entry.container_exec: _extract_existing_managed_routes(entry.container_exec, float(args.command_timeout_s))
        for entry in entries
    }
    next_state, results = apply_routes_incremental(
        desired_by_node=desired,
        entries=entries,
        applied_by_container=applied_by_container,
        route_dev=str(args.route_dev or ""),
        dry_run=bool(args.dry_run),
        timeout_s=float(args.command_timeout_s),
        workers=int(args.workers),
    )

    ok = sum(1 for item in results if item.ok)
    bad = len(results) - ok
    upserts = sum(item.upserts for item in results)
    deletes = sum(item.deletes for item in results)
    print(
        f"slot_index={slot.get('slot_index')} apply_ok={ok} apply_fail={bad} "
        f"route_upserts={upserts} route_deletes={deletes}"
    )
    for item in results:
        if not item.ok:
            print(f"[error] container={item.container} msg={item.error}")

    if str(args.state_output).strip():
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
            "dry_run": bool(args.dry_run),
            "applied_state": next_state if bad == 0 else {},
        }
        _write_state(Path(args.state_output).expanduser(), payload)

    return 0 if bad == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
