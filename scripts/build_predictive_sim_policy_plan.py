#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from dynamic_topo.engine import SimulationConfig, TopologyEngine
from push_sim_policy import (
    _build_policy,
    _load_existing_policy_extra,
    frame_edges,
    load_node_entries,
    resolve_sim_container,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an ephemeris-driven predictive simulator policy plan from future topology windows."
    )
    parser.add_argument("--mapping-csv", default="docs/node_mapping_300.csv", help="Node mapping CSV path")
    parser.add_argument(
        "--sim-container",
        default="auto",
        help="Simulator container name/id, or auto to infer <prefix>_sim from mapping-csv",
    )
    parser.add_argument("--sim-policy-path", default="/opt/sim/policy.json", help="Policy JSON path in simulator")
    parser.add_argument("--sim-proc-pattern", default="python3 /opt/sim/l2_center_sim.py")
    parser.add_argument("--node-mac-ifname", default="veth_0", help="Interface name used as node MAC source")
    parser.add_argument("--default-action", default="drop", help="Simulator default_action for no-match traffic")
    parser.add_argument("--workers", type=int, default=16, help="Parallel workers for container MAC resolution")
    parser.add_argument("--command-timeout-s", type=float, default=30.0, help="Timeout for each docker command")
    parser.add_argument(
        "--preserve-existing-extra",
        action="store_true",
        help="Preserve existing top-level fields in policy (except rules/default_action)",
    )
    parser.add_argument("--start-offset-s", type=float, default=0.0, help="Plan start offset from simulation epoch")
    parser.add_argument("--horizon-s", type=float, default=300.0, help="How far into the future to plan")
    parser.add_argument(
        "--sample-interval-s",
        type=float,
        default=30.0,
        help="How often to capture a planning sample; must be a multiple of engine timestep",
    )
    parser.add_argument("--engine-timestep-s", type=float, default=1.0, help="TopologyEngine timestep")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic engine seed")
    parser.add_argument("--link-policy", default="", help="Optional link policy JSON passed to TopologyEngine")
    parser.add_argument(
        "--output",
        default="run/predictive_sim_policy_plan.json",
        help="Output predictive sim policy plan JSON path",
    )
    return parser.parse_args()


def _validate_time_args(args: argparse.Namespace) -> tuple[int, int, int]:
    timestep_s = float(args.engine_timestep_s)
    start_offset_s = float(args.start_offset_s)
    horizon_s = float(args.horizon_s)
    sample_interval_s = float(args.sample_interval_s)
    if timestep_s <= 0.0:
        raise ValueError("engine-timestep-s must be > 0")
    if start_offset_s < 0.0:
        raise ValueError("start-offset-s must be >= 0")
    if horizon_s <= 0.0:
        raise ValueError("horizon-s must be > 0")
    if sample_interval_s <= 0.0:
        raise ValueError("sample-interval-s must be > 0")

    start_step = int(round(start_offset_s / timestep_s))
    total_steps = int(round((start_offset_s + horizon_s) / timestep_s))
    sample_every = int(round(sample_interval_s / timestep_s))
    if not math.isclose(start_step * timestep_s, start_offset_s, abs_tol=1e-9):
        raise ValueError("start-offset-s must be an integer multiple of engine-timestep-s")
    if not math.isclose(sample_every * timestep_s, sample_interval_s, abs_tol=1e-9):
        raise ValueError("sample-interval-s must be an integer multiple of engine-timestep-s")
    return start_step, total_steps, sample_every


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _component_summary(node_ids: list[str], edges: set[tuple[str, str]]) -> tuple[int, int]:
    adj = {node_id: set() for node_id in node_ids}
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)

    seen: set[str] = set()
    component_count = 0
    largest_component = 0
    for node_id in node_ids:
        if node_id in seen:
            continue
        component_count += 1
        size = 0
        q: deque[str] = deque([node_id])
        seen.add(node_id)
        while q:
            cur = q.popleft()
            size += 1
            for nxt in sorted(adj[cur]):
                if nxt in seen:
                    continue
                seen.add(nxt)
                q.append(nxt)
        largest_component = max(largest_component, size)
    return component_count, largest_component


def _policy_key(policy: dict[str, Any]) -> str:
    return json.dumps(policy, sort_keys=True, separators=(",", ":"))


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    start_step, total_steps, sample_every = _validate_time_args(args)
    cfg = SimulationConfig(
        timestep_s=float(args.engine_timestep_s),
        link_policy_path=str(args.link_policy or "") or None,
        link_policy_hot_reload=False,
    )
    engine = TopologyEngine(config=cfg, seed=int(args.seed))
    entries = load_node_entries(args)
    known_nodes = {entry.node_id for entry in entries}

    sim_container_hint = ""
    policy_extra: dict[str, Any] = {}
    if bool(args.preserve_existing_extra):
        sim_container_hint = resolve_sim_container(args)
        args.sim_container = sim_container_hint
        policy_extra = _load_existing_policy_extra(args)
    else:
        try:
            sim_container_hint = resolve_sim_container(args)
        except Exception:
            sim_container_hint = ""

    node_payload = {
        entry.node_id: {
            "node_id": entry.node_id,
            "node_index": entry.node_index,
            "container_name": entry.container_name,
            "container_exec": entry.container_exec,
            "node_mac": entry.node_mac,
        }
        for entry in entries
    }

    sample_summaries: list[dict[str, Any]] = []
    slots: list[dict[str, Any]] = []
    active_slot: dict[str, Any] | None = None
    active_key = ""

    for step_idx in range(total_steps + 1):
        sim_time_s = step_idx * float(args.engine_timestep_s)
        result = engine.step(sim_time_s, persist=False)
        if step_idx < start_step:
            continue
        if (step_idx - start_step) % sample_every != 0:
            continue

        frame = engine.build_frame(result)
        edges = frame_edges({"links": frame.links}, known_nodes)
        policy = _build_policy(
            edges=edges,
            entries=entries,
            default_action=str(args.default_action),
            policy_extra=policy_extra,
        )
        node_ids = sorted(known_nodes)
        component_count, largest_component_size = _component_summary(node_ids=node_ids, edges=edges)
        rule_count = len(policy.get("rules", [])) if isinstance(policy.get("rules"), list) else 0
        offset_s = sim_time_s - float(args.start_offset_s)
        sample_index = len(sample_summaries)
        sample_summaries.append(
            {
                "sample_index": sample_index,
                "sim_time_s": sim_time_s,
                "offset_s": offset_s,
                "edge_count": len(edges),
                "component_count": component_count,
                "largest_component_size": largest_component_size,
                "rule_count": rule_count,
            }
        )

        key = _policy_key(policy)
        if active_slot is None or key != active_key:
            active_slot = {
                "slot_index": len(slots),
                "start_offset_s": offset_s,
                "end_offset_s": offset_s,
                "start_sample_index": sample_index,
                "end_sample_index": sample_index,
                "sample_count": 1,
                "edge_count_min": len(edges),
                "edge_count_max": len(edges),
                "component_count_min": component_count,
                "component_count_max": component_count,
                "largest_component_size_min": largest_component_size,
                "largest_component_size_max": largest_component_size,
                "rule_count": rule_count,
                "policy": policy,
            }
            slots.append(active_slot)
            active_key = key
        else:
            active_slot["end_offset_s"] = offset_s
            active_slot["end_sample_index"] = sample_index
            active_slot["sample_count"] += 1
            active_slot["edge_count_min"] = min(active_slot["edge_count_min"], len(edges))
            active_slot["edge_count_max"] = max(active_slot["edge_count_max"], len(edges))
            active_slot["component_count_min"] = min(active_slot["component_count_min"], component_count)
            active_slot["component_count_max"] = max(active_slot["component_count_max"], component_count)
            active_slot["largest_component_size_min"] = min(
                active_slot["largest_component_size_min"], largest_component_size
            )
            active_slot["largest_component_size_max"] = max(
                active_slot["largest_component_size_max"], largest_component_size
            )

    if not sample_summaries:
        raise ValueError("predictive sim policy plan produced no samples")

    return {
        "schema": "dynamic_topo.predictive_sim_policy_plan.v1",
        "generated_at": _iso_utc_now(),
        "seed": int(args.seed),
        "mapping_csv": str(Path(args.mapping_csv)),
        "node_count": len(entries),
        "sim_container_hint": sim_container_hint,
        "sim_policy_path": str(args.sim_policy_path),
        "default_action": str(args.default_action),
        "start_offset_s": float(args.start_offset_s),
        "horizon_s": float(args.horizon_s),
        "sample_interval_s": float(args.sample_interval_s),
        "engine_timestep_s": float(args.engine_timestep_s),
        "sample_count": len(sample_summaries),
        "slot_count": len(slots),
        "nodes": node_payload,
        "samples": sample_summaries,
        "slots": slots,
    }


def main() -> int:
    args = parse_args()
    plan = build_plan(args)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(plan, ensure_ascii=False, sort_keys=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"output={out_path} nodes={plan['node_count']} samples={plan['sample_count']} "
        f"slots={plan['slot_count']} horizon_s={plan['horizon_s']} sample_interval_s={plan['sample_interval_s']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
