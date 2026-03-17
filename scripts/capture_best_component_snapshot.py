#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


def resolve_ws_connect():
    try:
        from websockets.asyncio.client import connect as ws_connect  # type: ignore

        return ws_connect
    except Exception:
        from websockets import connect as ws_connect  # type: ignore

        return ws_connect


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture the best connected topology frame from dynamic-topo.")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8765", help="Dynamic topology websocket URL")
    parser.add_argument("--mapping-csv", default="docs/node_mapping_300.csv", help="Node mapping CSV path")
    parser.add_argument("--max-nodes", type=int, default=300, help="How many rows from mapping CSV to consider")
    parser.add_argument("--output", required=True, help="Output JSON snapshot path")
    parser.add_argument("--sample-frames", type=int, default=20, help="How many frames to sample before choosing best")
    parser.add_argument("--reconnect-s", type=float, default=1.5, help="Reconnect delay after websocket errors")
    parser.add_argument(
        "--respect-proxy",
        action="store_true",
        help="Allow websockets client to use proxy env vars (default: disable proxy for WS)",
    )
    return parser.parse_args()


def load_node_ids(mapping_csv: Path, max_nodes: int) -> list[str]:
    with mapping_csv.open("r", encoding="utf-8", newline="") as fp:
        rows = list(csv.DictReader(fp))
    if not rows:
        raise SystemExit(f"[fatal] empty mapping csv: {mapping_csv}")
    rows.sort(key=lambda row: int(row["node_index"]))
    return [str(row["node_id"]) for row in rows[: max(1, int(max_nodes))]]


def frame_edges(frame: dict, known_nodes: set[str]) -> list[tuple[str, str]]:
    links = frame.get("links")
    if not isinstance(links, list):
        return []
    edges: set[tuple[str, str]] = set()
    for edge in links:
        if not isinstance(edge, dict):
            continue
        a = str(edge.get("a") or "")
        b = str(edge.get("b") or "")
        if a not in known_nodes or b not in known_nodes or a == b:
            continue
        if a <= b:
            edges.add((a, b))
        else:
            edges.add((b, a))
    return sorted(edges)


def largest_component(node_ids: list[str], edges: list[tuple[str, str]]) -> list[str]:
    adj: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)

    best: list[str] = []
    seen: set[str] = set()
    for node_id in node_ids:
        if node_id in seen:
            continue
        comp: list[str] = []
        q: deque[str] = deque([node_id])
        seen.add(node_id)
        while q:
            cur = q.popleft()
            comp.append(cur)
            for nxt in sorted(adj[cur]):
                if nxt in seen:
                    continue
                seen.add(nxt)
                q.append(nxt)
        if len(comp) > len(best):
            best = comp
    return best


async def capture_best(args: argparse.Namespace) -> dict:
    node_ids = load_node_ids(Path(args.mapping_csv), max_nodes=int(args.max_nodes))
    known_nodes = set(node_ids)
    ws_connect = resolve_ws_connect()

    if not args.respect_proxy:
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    sampled = 0
    best_payload: dict | None = None
    best_size = -1

    while sampled < int(args.sample_frames):
        try:
            kwargs = {}
            try:
                import inspect

                if "proxy" in inspect.signature(ws_connect).parameters:
                    kwargs["proxy"] = None
            except Exception:
                pass

            async with ws_connect(args.ws_url, **kwargs) as websocket:
                while sampled < int(args.sample_frames):
                    message = await websocket.recv()
                    frame = json.loads(message)
                    if not isinstance(frame, dict):
                        continue
                    edges = frame_edges(frame, known_nodes)
                    component_nodes = largest_component(node_ids, edges)
                    sampled += 1
                    if len(component_nodes) > best_size:
                        best_size = len(component_nodes)
                        best_payload = {
                            "schema": "dynamic_topo.best_component_snapshot.v1",
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                            "best_frame_index": frame.get("frame_index"),
                            "sampled_frames": sampled,
                            "node_count": len(node_ids),
                            "edge_count": len(edges),
                            "best_component_size": len(component_nodes),
                            "component_nodes": component_nodes,
                            "edges": [{"a": a, "b": b} for a, b in edges],
                        }
                        print(
                            f"sample={sampled} best_component={len(component_nodes)} "
                            f"frame={frame.get('frame_index')} edges={len(edges)}",
                            flush=True,
                        )
        except Exception as exc:
            print(f"ws error: {exc}; reconnecting in {args.reconnect_s}s", flush=True)
            await asyncio.sleep(float(args.reconnect_s))

    if best_payload is None:
        raise SystemExit("[fatal] failed to capture any topology frame")
    return best_payload


def main() -> int:
    args = parse_args()
    payload = asyncio.run(capture_best(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(
        f"output={output} best_component={payload['best_component_size']} "
        f"sampled_frames={payload['sampled_frames']} edge_count={payload['edge_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
