#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from dynamic_topo.engine import SimulationConfig, TopologyEngine
from dynamic_topo.storage import InMemoryRedis


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate deterministic topology snapshot baseline.")
    parser.add_argument("--steps", type=int, default=6, help="Number of simulation steps")
    parser.add_argument("--dt", type=float, default=1.0, help="Simulation timestep in seconds")
    parser.add_argument("--seed", type=int, default=7, help="Simulation RNG seed")
    parser.add_argument("--output", default="tests/fixtures/topology_snapshot.json", help="Snapshot output path")
    return parser.parse_args()


def hash_array(arr: np.ndarray) -> str:
    return hashlib.sha1(arr.tobytes()).hexdigest()


def build_snapshot(steps: int, dt: float, seed: int) -> dict:
    cfg = SimulationConfig(timestep_s=dt)
    engine = TopologyEngine(cfg, seed=seed, redis_client=InMemoryRedis())
    results = engine.run_steps(steps=steps, start_time_s=0.0, persist=False)

    frames: list[dict] = []
    for result in results:
        frame = engine.build_frame(result)
        adj_bits = np.packbits(result.adjacency.astype(np.uint8), bitorder="little")
        pos_round = np.round(result.node_positions_ecef, 3).astype(np.float64)
        links = sorted((min(link["a"], link["b"]), max(link["a"], link["b"])) for link in frame.links)

        frames.append(
            {
                "sim_time_s": float(result.sim_time_s),
                "edge_count": int(frame.metrics["edge_count"]),
                "avg_degree": float(round(frame.metrics["avg_degree"], 6)),
                "max_degree": int(frame.metrics["max_degree"]),
                "mobile_connected_count": int(frame.metrics.get("mobile_connected_count", 0)),
                "mobile_connected_ratio": float(round(frame.metrics.get("mobile_connected_ratio", 0.0), 6)),
                "link_flip_count_tick": int(frame.metrics.get("link_flip_count_tick", 0)),
                "adjacency_sha1": hash_array(adj_bits),
                "position_sha1": hash_array(pos_round),
                "first_12_links": [f"{a}-{b}" for a, b in links[:12]],
            }
        )

    return {
        "meta": {
            "seed": seed,
            "steps": steps,
            "dt": dt,
            "total_nodes": cfg.total_nodes,
            "node_counts": {
                "leo_polar": cfg.leo_polar_count,
                "leo_inclined": cfg.leo_inclined_count,
                "aircraft": cfg.aircraft_count,
                "ship": cfg.ship_count,
            },
        },
        "frames": frames,
    }


def main() -> None:
    args = parse_args()
    snapshot = build_snapshot(steps=args.steps, dt=args.dt, seed=args.seed)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
