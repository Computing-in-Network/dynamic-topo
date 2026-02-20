from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from time import perf_counter

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
    redis_queue: asyncio.Queue[tuple[float, object, object]] = asyncio.Queue(maxsize=2)

    async def handler(websocket):
        clients.add(websocket)
        try:
            async for _ in websocket:
                # Control channel reserved for future use.
                pass
        finally:
            clients.discard(websocket)

    async def redis_writer() -> None:
        while True:
            sim_time, positions, adjacency = await redis_queue.get()
            await asyncio.to_thread(engine.persist_state, sim_time, positions, adjacency)
            redis_queue.task_done()

    async def producer() -> None:
        sim_time = 0.0
        dt = config.timestep_s
        next_deadline = perf_counter()
        tick_count = 0
        lag_ms_window: list[float] = []
        compute_ms_window: list[float] = []
        while True:
            tick_start = perf_counter()
            result = engine.step(sim_time, persist=False)
            frame = engine.build_frame(result)
            payload = json.dumps(asdict(frame), separators=(",", ":"))
            if clients:
                await asyncio.gather(*(ws.send(payload) for ws in list(clients)), return_exceptions=True)

            # Decouple Redis I/O from compute loop: keep only latest states if writer lags.
            if redis_queue.full():
                try:
                    _ = redis_queue.get_nowait()
                    redis_queue.task_done()
                except asyncio.QueueEmpty:
                    pass
            await redis_queue.put((sim_time, result.node_positions_ecef, result.adjacency))

            tick_count += 1
            compute_ms_window.append(result.elapsed_ms)

            next_deadline += dt
            now = perf_counter()
            lag_ms = max(0.0, (now - next_deadline) * 1000.0)
            lag_ms_window.append(lag_ms)
            sleep_s = next_deadline - now
            if sleep_s > 0.0:
                await asyncio.sleep(sleep_s)
            else:
                # If late, reset deadline to avoid accumulating drift.
                next_deadline = perf_counter()

            if tick_count % 10 == 0:
                lag_sorted = sorted(lag_ms_window)
                p95_idx = max(0, int(0.95 * (len(lag_sorted) - 1)))
                p95_lag = lag_sorted[p95_idx]
                avg_compute = sum(compute_ms_window) / max(1, len(compute_ms_window))
                loop_ms = (perf_counter() - tick_start) * 1000.0
                print(
                    f"tick={tick_count} avg_compute={avg_compute:.2f}ms "
                    f"loop={loop_ms:.2f}ms lag_p95={p95_lag:.2f}ms lag_max={max(lag_ms_window):.2f}ms"
                )
                lag_ms_window.clear()
                compute_ms_window.clear()

            sim_time += config.timestep_s

    async with serve(handler, host, port, max_size=10_000_000):
        print(f"ws://{host}:{port} serving topology stream, dt={config.timestep_s:.2f}s")
        writer_task = asyncio.create_task(redis_writer())
        try:
            await producer()
        finally:
            writer_task.cancel()
            await asyncio.gather(writer_task, return_exceptions=True)


def main() -> None:
    args = parse_args()
    config = SimulationConfig(timestep_s=args.dt)
    asyncio.run(run_server(args.host, args.port, config=config, seed=args.seed))


if __name__ == "__main__":
    main()
