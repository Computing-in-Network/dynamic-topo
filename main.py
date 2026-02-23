from __future__ import annotations

import argparse
import time

from dynamic_topo import SimulationConfig, TopologyEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="300-node dynamic topology simulator")
    parser.add_argument("--steps", type=int, default=5, help="Number of ticks to run")
    parser.add_argument("--dt", type=float, default=1.0, help="Simulation step in seconds")
    parser.add_argument("--link-policy", default=None, help="Path to link policy JSON file")
    parser.add_argument(
        "--hot-reload-link-policy",
        action="store_true",
        help="Reload link policy file when it changes",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = SimulationConfig(
        timestep_s=args.dt,
        link_policy_path=args.link_policy,
        link_policy_hot_reload=args.hot_reload_link_policy,
    )
    engine = TopologyEngine(config)

    sim_time = 0.0
    for _ in range(args.steps):
        result = engine.step(sim_time)
        symmetric = bool((result.adjacency == result.adjacency.T).all())
        print(
            f"t={result.sim_time_s:6.1f}s nodes={config.total_nodes} "
            f"sym={symmetric} elapsed={result.elapsed_ms:.2f}ms"
        )
        sim_time += config.timestep_s
        time.sleep(max(0.0, config.timestep_s))


if __name__ == "__main__":
    main()
