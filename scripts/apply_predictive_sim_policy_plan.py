#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from push_sim_policy import _write_and_reload_policy, resolve_sim_container


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply one slot from a predictive simulator policy plan into the simulator container."
    )
    parser.add_argument("--plan", required=True, help="Predictive sim policy plan JSON path")
    parser.add_argument("--slot-index", type=int, default=-1, help="Slot index to apply")
    parser.add_argument(
        "--at-offset-s",
        type=float,
        default=-1.0,
        help="Pick the slot that covers this relative offset instead of explicit slot-index",
    )
    parser.add_argument(
        "--sim-container",
        default="auto",
        help="Simulator container name/id, or auto to use plan hint / inferred container",
    )
    parser.add_argument("--mapping-csv", default="", help="Optional mapping CSV for auto sim-container resolution")
    parser.add_argument("--sim-policy-path", default="", help="Override simulator policy path")
    parser.add_argument("--sim-proc-pattern", default="python3 /opt/sim/l2_center_sim.py")
    parser.add_argument("--output-policy", default="", help="Optional local path to write policy snapshot")
    parser.add_argument("--command-timeout-s", type=float, default=30.0, help="Timeout for each docker command")
    parser.add_argument("--dry-run", action="store_true", help="Print summary only, do not write simulator policy")
    parser.add_argument(
        "--state-output",
        default="",
        help="Optional JSON path recording the last applied predictive sim policy slot",
    )
    return parser.parse_args()


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("plan root must be a JSON object")
    if payload.get("schema") != "dynamic_topo.predictive_sim_policy_plan.v1":
        raise ValueError(f"unsupported predictive sim policy plan schema: {payload.get('schema')}")
    return payload


def _select_slot(plan: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    slots = plan.get("slots")
    if not isinstance(slots, list) or not slots:
        raise ValueError("predictive sim policy plan has no slots")
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
        raise ValueError(f"no predictive sim policy slot covers at-offset-s={offset_s}")
    slot = slots[0]
    if not isinstance(slot, dict):
        raise ValueError("invalid slot at index 0")
    return slot


def _resolve_sim_container(plan: dict[str, Any], args: argparse.Namespace) -> str:
    requested = str(args.sim_container).strip()
    if requested and requested.lower() != "auto":
        return requested
    hint = str(plan.get("sim_container_hint", "")).strip()
    if hint:
        return hint
    mapping_csv = str(args.mapping_csv or plan.get("mapping_csv") or "").strip()
    ns = SimpleNamespace(
        sim_container="auto",
        mapping_csv=mapping_csv,
        command_timeout_s=float(args.command_timeout_s),
    )
    return resolve_sim_container(ns)


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    plan_path = Path(args.plan).expanduser()
    plan = _load_plan(plan_path)
    slot = _select_slot(plan, args)
    policy = slot.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("predictive sim policy slot missing policy")

    sim_container = _resolve_sim_container(plan, args)
    sim_policy_path = str(args.sim_policy_path or plan.get("sim_policy_path") or "/opt/sim/policy.json")
    apply_args = SimpleNamespace(
        sim_container=sim_container,
        sim_policy_path=sim_policy_path,
        sim_proc_pattern=str(args.sim_proc_pattern),
        output_policy=str(args.output_policy),
        command_timeout_s=float(args.command_timeout_s),
        dry_run=bool(args.dry_run),
    )
    res = _write_and_reload_policy(apply_args, policy)
    status = "ok" if res.ok else "fail"
    print(f"slot_index={slot.get('slot_index')} sim={sim_container} rules={res.rule_count} policy_apply={status}")
    if not res.ok:
        print(f"[error] {res.error}")

    if str(args.state_output).strip():
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
            "dry_run": bool(args.dry_run),
            "error": res.error,
        }
        _write_state(Path(args.state_output).expanduser(), payload)

    return 0 if res.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
