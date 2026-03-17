#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from dynamic_topo.engine import SimulationConfig, TopologyEngine
from push_static_routes import desired_routes_from_edges, frame_edges, load_node_entries


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an ephemeris-driven predictive route plan from future topology windows."
    )
    parser.add_argument("--mapping-csv", default="docs/node_mapping_300.csv", help="Node mapping CSV path")
    parser.add_argument(
        "--container-ip-fields",
        default="container_ip,mgmt_ip,node_ip,host_ip",
        help="CSV fields used to locate container next-hop IP (comma separated)",
    )
    parser.add_argument(
        "--container-ip-source",
        default="csv-or-docker",
        choices=("csv", "docker", "csv-or-docker"),
        help="How to resolve container next-hop IPs",
    )
    parser.add_argument("--docker-network", default="", help="Specific docker network name when using inspect")
    parser.add_argument(
        "--loopback-base-cidr",
        default="10.200.0.0/16",
        help="Base network for per-node destination prefixes when runtime loopback is unavailable",
    )
    parser.add_argument("--loopback-prefix-len", type=int, default=32, help="Prefix length for per-node destination")
    parser.add_argument("--command-timeout-s", type=float, default=30.0, help="Timeout for each docker command")
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
        default="run/predictive_route_plan.json",
        help="Output predictive route plan JSON path",
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


def _component_summary(
    node_ids: list[str],
    edges: set[tuple[str, str]],
) -> tuple[int, int]:
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


def _plan_key(desired_routes: dict[str, dict[str, str]]) -> str:
    return json.dumps(desired_routes, sort_keys=True, separators=(",", ":"))


def _slot_summary(
    *,
    sample_index: int,
    sim_time_s: float,
    offset_s: float,
    edges: set[tuple[str, str]],
    edge_count: int,
    component_sizes: dict[str, int],
    desired_routes: dict[str, dict[str, str]],
    route_hops: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    node_ids = sorted(component_sizes.keys())
    component_count, largest_component_size = _component_summary(node_ids=node_ids, edges=edges)
    return {
        "sample_index": sample_index,
        "sim_time_s": sim_time_s,
        "offset_s": offset_s,
        "edge_count": edge_count,
        "component_count": component_count,
        "largest_component_size": largest_component_size,
        "desired_routes": desired_routes,
        "route_hops": route_hops,
        "component_sizes": component_sizes,
    }


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
    node_payload = {
        entry.node_id: {
            "node_id": entry.node_id,
            "node_index": entry.node_index,
            "container_name": entry.container_name,
            "container_exec": entry.container_exec,
            "container_ip": entry.container_ip,
            "loopback_prefix": entry.loopback_prefix,
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
        desired_routes, route_hops, component_sizes = desired_routes_from_edges(edges, entries)
        route_hops_payload = {
            src: {dst: asdict(hop) for dst, hop in hops.items()}
            for src, hops in route_hops.items()
        }
        offset_s = sim_time_s - float(args.start_offset_s)
        summary = _slot_summary(
            sample_index=len(sample_summaries),
            sim_time_s=sim_time_s,
            offset_s=offset_s,
            edges=edges,
            edge_count=len(edges),
            component_sizes=component_sizes,
            desired_routes=desired_routes,
            route_hops=route_hops_payload,
        )
        sample_summaries.append(
            {
                "sample_index": summary["sample_index"],
                "sim_time_s": summary["sim_time_s"],
                "offset_s": summary["offset_s"],
                "edge_count": summary["edge_count"],
                "component_count": summary["component_count"],
                "largest_component_size": summary["largest_component_size"],
            }
        )

        key = _plan_key(desired_routes)
        if active_slot is None or key != active_key:
            active_slot = {
                "slot_index": len(slots),
                "start_offset_s": offset_s,
                "end_offset_s": offset_s,
                "start_sample_index": summary["sample_index"],
                "end_sample_index": summary["sample_index"],
                "sample_count": 1,
                "edge_count_min": summary["edge_count"],
                "edge_count_max": summary["edge_count"],
                "component_count_min": summary["component_count"],
                "component_count_max": summary["component_count"],
                "largest_component_size_min": summary["largest_component_size"],
                "largest_component_size_max": summary["largest_component_size"],
                "desired_routes": desired_routes,
                "route_hops": route_hops_payload,
                "component_sizes": component_sizes,
            }
            slots.append(active_slot)
            active_key = key
        else:
            active_slot["end_offset_s"] = offset_s
            active_slot["end_sample_index"] = summary["sample_index"]
            active_slot["sample_count"] += 1
            active_slot["edge_count_min"] = min(active_slot["edge_count_min"], summary["edge_count"])
            active_slot["edge_count_max"] = max(active_slot["edge_count_max"], summary["edge_count"])
            active_slot["component_count_min"] = min(
                active_slot["component_count_min"], summary["component_count"]
            )
            active_slot["component_count_max"] = max(
                active_slot["component_count_max"], summary["component_count"]
            )
            active_slot["largest_component_size_min"] = min(
                active_slot["largest_component_size_min"], summary["largest_component_size"]
            )
            active_slot["largest_component_size_max"] = max(
                active_slot["largest_component_size_max"], summary["largest_component_size"]
            )

    if not sample_summaries:
        raise ValueError("predictive route plan captured zero samples")

    return {
        "schema": "dynamic_topo.predictive_route_plan.v1",
        "generated_at": _iso_utc_now(),
        "seed": int(args.seed),
        "mapping_csv": str(args.mapping_csv),
        "node_count": len(entries),
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
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(
        f"output={output} nodes={plan['node_count']} samples={plan['sample_count']} slots={plan['slot_count']} "
        f"horizon_s={plan['horizon_s']} sample_interval_s={plan['sample_interval_s']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
