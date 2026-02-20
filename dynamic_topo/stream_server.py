from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from .engine import SimulationConfig, TopologyEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dynamic topology websocket stream server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument("--dt", type=float, default=1.0, help="Simulation tick seconds")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic RNG seed")
    return parser.parse_args()


async def run_server(host: str, port: int, config: SimulationConfig, seed: int) -> None:
    try:
        from websockets.asyncio.server import serve
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("websockets package is required. Install deps with `uv sync --dev`.") from exc

    engine = TopologyEngine(config=config, seed=seed)
    clients: set = set()

    async def handler(websocket):
        clients.add(websocket)
        try:
            async for _ in websocket:
                # Control channel reserved for future use.
                pass
        finally:
            clients.discard(websocket)

    async def producer() -> None:
        sim_time = 0.0
        while True:
            result = engine.step(sim_time)
            frame = engine.build_frame(result)
            payload = json.dumps(asdict(frame), separators=(",", ":"))
            if clients:
                await asyncio.gather(*(ws.send(payload) for ws in list(clients)), return_exceptions=True)
            sim_time += config.timestep_s
            await asyncio.sleep(config.timestep_s)

    async with serve(handler, host, port, max_size=10_000_000):
        print(f"ws://{host}:{port} serving topology stream, dt={config.timestep_s:.2f}s")
        await producer()


def main() -> None:
    args = parse_args()
    config = SimulationConfig(timestep_s=args.dt)
    asyncio.run(run_server(args.host, args.port, config=config, seed=args.seed))


if __name__ == "__main__":
    main()
