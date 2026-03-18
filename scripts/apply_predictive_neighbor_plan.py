#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


_NEIGH_LINE_RE = re.compile(r"^(?P<ip>[0-9.]+)\s+(?:dev\s+\S+\s+)?(?:lladdr\s+(?P<mac>[0-9a-f:]{17})\s+)?(?P<state>[A-Z_]+)")


@dataclass(frozen=True)
class NeighborPlanEntry:
    node_id: str
    node_index: int
    container_name: str
    container_exec: str
    container_ip: str
    node_mac: str


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    container: str
    upserts: int
    deletes: int
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply one slot from a predictive neighbor plan into containers."
    )
    parser.add_argument("--plan", required=True, help="Predictive neighbor plan JSON path")
    parser.add_argument("--slot-index", type=int, default=-1, help="Slot index to apply")
    parser.add_argument(
        "--at-offset-s",
        type=float,
        default=-1.0,
        help="Pick the slot that covers this relative offset instead of explicit slot-index",
    )
    parser.add_argument("--neighbor-dev", default="veth_0", help="Interface device used for permanent neighbors")
    parser.add_argument("--workers", type=int, default=8, help="Parallel container workers")
    parser.add_argument("--command-timeout-s", type=float, default=30.0, help="Timeout for each docker command")
    parser.add_argument("--dry-run", action="store_true", help="Print commands only, do not execute")
    parser.add_argument(
        "--state-output",
        default="",
        help="Optional JSON path recording the last applied predictive neighbor slot",
    )
    return parser.parse_args()


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("plan root must be a JSON object")
    if payload.get("schema") != "dynamic_topo.predictive_neighbor_plan.v1":
        raise ValueError(f"unsupported predictive neighbor plan schema: {payload.get('schema')}")
    return payload


def _select_slot(plan: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    slots = plan.get("slots")
    if not isinstance(slots, list) or not slots:
        raise ValueError("predictive neighbor plan has no slots")
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
        previous: dict[str, Any] | None = None
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            start_s = float(slot.get("start_offset_s", -1.0))
            if start_s <= offset_s:
                previous = slot
                continue
            break
        if previous is not None and offset_s <= float(plan.get("horizon_s", offset_s)):
            return previous
        raise ValueError(f"no predictive neighbor slot covers at-offset-s={offset_s}")
    slot = slots[0]
    if not isinstance(slot, dict):
        raise ValueError("invalid slot at index 0")
    return slot


def _entries_from_plan(plan: dict[str, Any]) -> list[NeighborPlanEntry]:
    nodes = plan.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError("predictive neighbor plan missing nodes payload")
    entries: list[NeighborPlanEntry] = []
    for node_id, payload in nodes.items():
        if not isinstance(payload, dict):
            continue
        entries.append(
            NeighborPlanEntry(
                node_id=str(node_id),
                node_index=int(payload["node_index"]),
                container_name=str(payload.get("container_name", "")),
                container_exec=str(payload["container_exec"]),
                container_ip=str(payload["container_ip"]),
                node_mac=str(payload["node_mac"]),
            )
        )
    entries.sort(key=lambda entry: entry.node_index)
    return entries


def _desired_neighbors_from_slot(slot: dict[str, Any]) -> dict[str, dict[str, dict[str, str]]]:
    desired = slot.get("desired_neighbors")
    if not isinstance(desired, dict):
        raise ValueError("predictive neighbor slot missing desired_neighbors")
    normalized: dict[str, dict[str, dict[str, str]]] = {}
    for node_id, neighbors in desired.items():
        if not isinstance(neighbors, dict):
            continue
        normalized[str(node_id)] = {}
        for ip, payload in neighbors.items():
            if not isinstance(payload, dict):
                continue
            normalized[str(node_id)][str(ip)] = {
                "neighbor_ip": str(payload.get("neighbor_ip") or ip),
                "neighbor_mac": str(payload["neighbor_mac"]),
                "neighbor_node": str(payload.get("neighbor_node", "")),
            }
    return normalized


def _extract_existing_managed_neighbors(
    container: str,
    *,
    neighbor_dev: str,
    timeout_s: float,
    managed_ip_set: set[str],
) -> dict[str, dict[str, str]]:
    proc = subprocess.run(
        ["docker", "exec", container, "ip", "neigh", "show", "dev", neighbor_dev],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"failed to read neighbors from {container}: {err[:300]}")
    out: dict[str, dict[str, str]] = {}
    for raw in proc.stdout.splitlines():
        line = raw.strip().lower()
        if not line:
            continue
        m = _NEIGH_LINE_RE.match(line)
        if not m:
            continue
        ip = m.group("ip")
        if ip not in managed_ip_set:
            continue
        out[ip] = {
            "neighbor_ip": ip,
            "neighbor_mac": (m.group("mac") or "").lower(),
            "state": m.group("state").upper(),
        }
    return out


def _apply_one_container_neighbors(
    *,
    container: str,
    desired: dict[str, dict[str, str]],
    existing: dict[str, dict[str, str]],
    neighbor_dev: str,
    dry_run: bool,
    timeout_s: float,
) -> ApplyResult:
    deletes = sorted(set(existing) - set(desired))
    upserts: list[tuple[str, str]] = []
    for ip, payload in sorted(desired.items()):
        mac = str(payload["neighbor_mac"]).lower()
        current = existing.get(ip)
        if current is None or current.get("neighbor_mac", "").lower() != mac or current.get("state") != "PERMANENT":
            upserts.append((ip, mac))

    if dry_run:
        for ip in deletes:
            print(f"[dry-run] docker exec {container} ip neigh del {ip} dev {neighbor_dev}")
        for ip, mac in upserts:
            print(
                f"[dry-run] docker exec {container} ip neigh replace {ip} lladdr {mac} dev {neighbor_dev} nud permanent"
            )
        return ApplyResult(ok=True, container=container, upserts=len(upserts), deletes=len(deletes))

    if not deletes and not upserts:
        return ApplyResult(ok=True, container=container, upserts=0, deletes=0)

    shell_lines = ["set -eu"]
    for ip in deletes:
        shell_lines.append(f"ip neigh del {ip} dev {neighbor_dev} >/dev/null 2>&1 || true")
    for ip, mac in upserts:
        shell_lines.append(f"ip neigh replace {ip} lladdr {mac} dev {neighbor_dev} nud permanent")
    proc = subprocess.run(
        ["docker", "exec", container, "sh", "-lc", "\n".join(shell_lines)],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return ApplyResult(ok=False, container=container, upserts=len(upserts), deletes=len(deletes), error=err[:800])
    return ApplyResult(ok=True, container=container, upserts=len(upserts), deletes=len(deletes))


def apply_neighbors_incremental(
    *,
    desired_by_node: dict[str, dict[str, dict[str, str]]],
    entries: list[NeighborPlanEntry],
    applied_by_container: dict[str, dict[str, dict[str, str]]],
    neighbor_dev: str,
    dry_run: bool,
    timeout_s: float,
    workers: int,
) -> tuple[dict[str, dict[str, dict[str, str]]], list[ApplyResult]]:
    by_node = {entry.node_id: entry for entry in entries}
    next_state: dict[str, dict[str, dict[str, str]]] = {}
    jobs: list[tuple[str, dict[str, dict[str, str]], dict[str, dict[str, str]]]] = []
    for node_id, entry in by_node.items():
        desired = desired_by_node.get(node_id, {})
        existing = applied_by_container.get(entry.container_exec, {})
        jobs.append((entry.container_exec, desired, existing))
        next_state[entry.container_exec] = desired

    results: list[ApplyResult] = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futs = [
            pool.submit(
                _apply_one_container_neighbors,
                container=container,
                desired=desired,
                existing=existing,
                neighbor_dev=neighbor_dev,
                dry_run=dry_run,
                timeout_s=timeout_s,
            )
            for container, desired, existing in jobs
        ]
        for fut in futs:
            results.append(fut.result())
    results.sort(key=lambda item: item.container)
    return next_state, results


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    plan_path = Path(args.plan).expanduser()
    plan = _load_plan(plan_path)
    slot = _select_slot(plan, args)
    entries = _entries_from_plan(plan)
    desired = _desired_neighbors_from_slot(slot)
    managed_ip_set = {entry.container_ip for entry in entries}

    applied_by_container = {
        entry.container_exec: _extract_existing_managed_neighbors(
            entry.container_exec,
            neighbor_dev=str(args.neighbor_dev),
            timeout_s=float(args.command_timeout_s),
            managed_ip_set=managed_ip_set,
        )
        for entry in entries
    }
    next_state, results = apply_neighbors_incremental(
        desired_by_node=desired,
        entries=entries,
        applied_by_container=applied_by_container,
        neighbor_dev=str(args.neighbor_dev),
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
        f"neighbor_upserts={upserts} neighbor_deletes={deletes}"
    )
    for item in results:
        if not item.ok:
            print(f"[error] container={item.container} msg={item.error}")

    if str(args.state_output).strip():
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
            "dry_run": bool(args.dry_run),
            "applied_state": next_state if bad == 0 else {},
        }
        _write_state(Path(args.state_output).expanduser(), payload)

    return 0 if bad == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
