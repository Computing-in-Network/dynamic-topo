#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import inspect
import ipaddress
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class NodeEntry:
    node_id: str
    node_index: int
    container_name: str
    container_exec: str
    container_ip: str
    container_mac: str
    loopback_prefix: str


def resolve_ws_connect():
    try:
        from websockets.asyncio.client import connect as ws_connect  # type: ignore

        return ws_connect
    except Exception:
        from websockets import connect as ws_connect  # type: ignore

        return ws_connect


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Subscribe dynamic-topo websocket frames and push direct-neighbor state into containers."
    )
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8765", help="Dynamic topology websocket URL")
    parser.add_argument("--mapping-csv", default="docs/node_mapping_300.csv", help="Node mapping CSV path")
    parser.add_argument("--max-nodes", type=int, default=10, help="How many nodes from mapping CSV to use")
    parser.add_argument("--neighbor-state-path", default="/run/dv/neighbors.json", help="Neighbor JSON path in node")
    parser.add_argument("--neighbor-dev", default="veth_0", help="Node interface used for static neighbor entries")
    parser.add_argument("--min-stable-frames", type=int, default=2, help="Only apply after N identical frames")
    parser.add_argument(
        "--min-apply-interval-s",
        type=float,
        default=2.0,
        help="Minimum seconds between successful neighbor-state applies after the initial apply",
    )
    parser.add_argument("--workers", type=int, default=8, help="Parallel workers for container writes")
    parser.add_argument("--command-timeout-s", type=float, default=20.0, help="Timeout for docker commands")
    parser.add_argument("--reconnect-s", type=float, default=1.5, help="Reconnect delay after websocket errors")
    parser.add_argument(
        "--respect-proxy",
        action="store_true",
        help="Allow websockets client to use proxy env vars (default: disable proxy for WS)",
    )
    parser.add_argument("--state-output", default="", help="Optional local JSON snapshot path")
    parser.add_argument("--dry-run", action="store_true", help="Do not write neighbor state into containers")
    parser.add_argument("--once", action="store_true", help="Process the first stable frame then exit")
    return parser.parse_args()


def _load_mapping_rows(path: Path, max_nodes: int) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError(f"mapping csv is empty: {path}")
    rows.sort(key=lambda x: int(x["node_index"]))
    return rows[: max(1, int(max_nodes))]


def _run_cmd(cmd: list[str], timeout_s: float, *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def _inspect_one_container(ref: str, timeout_s: float) -> dict | None:
    proc = _run_cmd(["docker", "inspect", ref], timeout_s=timeout_s)
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None
    return payload[0]


def _extract_veth0_ip(ref: str, timeout_s: float) -> str:
    proc = _run_cmd(["docker", "exec", ref, "ip", "-o", "-4", "addr", "show", "dev", "veth_0"], timeout_s=timeout_s)
    if proc.returncode != 0:
        return ""
    for raw in proc.stdout.splitlines():
        parts = raw.split()
        if len(parts) >= 4 and parts[2] == "inet":
            return str(ipaddress.ip_interface(parts[3]).ip)
    return ""


def _extract_veth0_mac(ref: str, timeout_s: float) -> str:
    proc = _run_cmd(
        ["docker", "exec", ref, "sh", "-lc", "cat /sys/class/net/veth_0/address"],
        timeout_s=timeout_s,
    )
    if proc.returncode != 0:
        return ""
    return str(proc.stdout or "").strip().lower()


def _extract_loopback_prefix(ref: str, timeout_s: float) -> str:
    proc = _run_cmd(["docker", "exec", ref, "ip", "-o", "-4", "addr", "show", "dev", "lo"], timeout_s=timeout_s)
    if proc.returncode != 0:
        return ""
    for raw in proc.stdout.splitlines():
        parts = raw.split()
        if len(parts) >= 4 and parts[2] == "inet":
            prefix = str(ipaddress.ip_interface(parts[3]).network)
            if prefix != "127.0.0.0/8":
                return prefix
    return ""


def _resolve_node_entry(row: dict[str, str], timeout_s: float) -> NodeEntry:
    refs = [str(row.get("container_name") or "").strip(), str(row.get("container_id") or "").strip()]
    inspect_item = None
    for ref in refs:
        if not ref:
            continue
        inspect_item = _inspect_one_container(ref, timeout_s=timeout_s)
        if inspect_item is not None:
            break
    if inspect_item is None:
        raise ValueError(f"cannot inspect container for {row['node_id']}")

    actual_name = str(inspect_item.get("Name", "")).lstrip("/")
    short_id = str(inspect_item.get("Id", "")).strip()[:12]
    exec_target = actual_name or short_id
    if not exec_target:
        raise ValueError(f"cannot resolve exec target for {row['node_id']}")

    container_ip = _extract_veth0_ip(exec_target, timeout_s=timeout_s)
    container_mac = _extract_veth0_mac(exec_target, timeout_s=timeout_s)
    loopback_prefix = _extract_loopback_prefix(exec_target, timeout_s=timeout_s)
    if not container_ip:
        raise ValueError(f"missing veth_0 ipv4 for {row['node_id']}({exec_target})")
    if not container_mac:
        raise ValueError(f"missing veth_0 mac for {row['node_id']}({exec_target})")
    if not loopback_prefix:
        raise ValueError(f"missing loopback prefix for {row['node_id']}({exec_target})")

    return NodeEntry(
        node_id=str(row["node_id"]),
        node_index=int(row["node_index"]),
        container_name=str(row["container_name"]),
        container_exec=exec_target,
        container_ip=container_ip,
        container_mac=container_mac,
        loopback_prefix=loopback_prefix,
    )


def load_node_entries(args: argparse.Namespace) -> list[NodeEntry]:
    rows = _load_mapping_rows(Path(args.mapping_csv), max_nodes=int(args.max_nodes))
    timeout_s = float(args.command_timeout_s)
    workers = max(1, int(args.workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        items = [pool.submit(_resolve_node_entry, row, timeout_s) for row in rows]
        entries = [fut.result() for fut in items]
    entries.sort(key=lambda x: x.node_index)
    return entries


def _edge_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def frame_edges(frame: dict, known_nodes: set[str]) -> set[tuple[str, str]]:
    links = frame.get("links")
    if not isinstance(links, list):
        return set()
    edges: set[tuple[str, str]] = set()
    for edge in links:
        if not isinstance(edge, dict):
            continue
        a = str(edge.get("a") or "")
        b = str(edge.get("b") or "")
        if not a or not b or a == b:
            continue
        if a not in known_nodes or b not in known_nodes:
            continue
        edges.add(_edge_key(a, b))
    return edges


def build_neighbor_payloads(
    *,
    edges: set[tuple[str, str]],
    entries: list[NodeEntry],
    frame_idx: int,
) -> dict[str, str]:
    by_node = {e.node_id: e for e in entries}
    neighbors: dict[str, list[dict[str, str]]] = {e.node_id: [] for e in entries}
    for a, b in sorted(edges):
        ea = by_node[a]
        eb = by_node[b]
        neighbors[a].append(
            {"node_id": eb.node_id, "ip": eb.container_ip, "mac": eb.container_mac, "prefix": eb.loopback_prefix}
        )
        neighbors[b].append(
            {"node_id": ea.node_id, "ip": ea.container_ip, "mac": ea.container_mac, "prefix": ea.loopback_prefix}
        )

    out: dict[str, str] = {}
    generated_at = datetime.now(timezone.utc).isoformat()
    for entry in entries:
        payload = {
            "schema": "dynamic_topo.dv_neighbors.v1",
            "generated_at": generated_at,
            "frame_index": frame_idx,
            "node_id": entry.node_id,
            "local_ip": entry.container_ip,
            "local_prefix": entry.loopback_prefix,
            "neighbors": sorted(neighbors[entry.node_id], key=lambda x: x["node_id"]),
        }
        out[entry.container_exec] = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return out


def _write_neighbor_file(container: str, payload: str, path: str, timeout_s: float) -> tuple[bool, str]:
    tmp_path = f"{path}.tmp"
    mkdir_cmd = ["docker", "exec", container, "sh", "-lc", f"mkdir -p {Path(path).parent.as_posix()}"]
    proc = _run_cmd(mkdir_cmd, timeout_s=timeout_s)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, f"mkdir failed: {err[:400]}"

    proc = _run_cmd(["docker", "exec", "-i", container, "sh", "-lc", f"cat > {tmp_path}"], timeout_s=timeout_s, input_text=payload)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, f"write failed: {err[:400]}"

    proc = _run_cmd(["docker", "exec", container, "sh", "-lc", f"mv {tmp_path} {path}"], timeout_s=timeout_s)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, f"rename failed: {err[:400]}"
    return True, ""


def _sync_static_neighbors(
    *,
    entry: NodeEntry,
    desired_neighbors: list[dict[str, str]],
    known_entries: list[NodeEntry],
    neighbor_dev: str,
    timeout_s: float,
) -> tuple[bool, str]:
    lines = ["set -eu"]
    for item in known_entries:
        if item.node_id == entry.node_id:
            continue
        lines.append(f"ip neigh del {item.container_ip} dev {neighbor_dev} >/dev/null 2>&1 || true")
    for item in desired_neighbors:
        ip = str(item["ip"])
        mac = str(item["mac"]).lower()
        lines.append(f"ip neigh replace {ip} lladdr {mac} dev {neighbor_dev} nud permanent")
    shell = "; ".join(lines)
    proc = _run_cmd(["docker", "exec", entry.container_exec, "sh", "-lc", shell], timeout_s=timeout_s)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, f"neighbor sync failed: {err[:400]}"
    return True, ""


def _persist_state_snapshot(
    *,
    path: Path,
    frame_idx: int,
    edges: set[tuple[str, str]],
    entries: list[NodeEntry],
) -> None:
    snapshot = {
        "schema": "dynamic_topo.dv_neighbor_snapshot.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "frame_index": frame_idx,
        "edge_count": len(edges),
        "node_count": len(entries),
        "edges": [{"a": a, "b": b} for a, b in sorted(edges)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ws_connect_kwargs(ws_connect, respect_proxy: bool) -> dict:
    kwargs = {"max_size": 20_000_000}
    if respect_proxy:
        return kwargs
    try:
        params = inspect.signature(ws_connect).parameters
        if "proxy" in params:
            kwargs["proxy"] = None
    except Exception:
        pass
    return kwargs


async def run_controller(args: argparse.Namespace) -> int:
    ws_connect = resolve_ws_connect()
    entries = load_node_entries(args)
    known_nodes = {e.node_id for e in entries}
    pending_edges: set[tuple[str, str]] | None = None
    pending_count = 0
    committed_edges: set[tuple[str, str]] | None = None
    frame_idx = 0
    last_apply_at = 0.0
    min_apply_interval_s = max(0.0, float(args.min_apply_interval_s))
    timeout_s = float(args.command_timeout_s)

    print(
        f"loaded nodes={len(entries)} ws={args.ws_url} neighbor_state={args.neighbor_state_path} "
        f"dry_run={args.dry_run} min_stable_frames={args.min_stable_frames} "
        f"min_apply_interval_s={args.min_apply_interval_s}"
    )

    ws_kwargs = _ws_connect_kwargs(ws_connect, respect_proxy=bool(args.respect_proxy))
    if "proxy" in ws_kwargs and ws_kwargs["proxy"] is None:
        print("ws proxy disabled (pass --respect-proxy to override)")

    while True:
        try:
            async with ws_connect(args.ws_url, **ws_kwargs) as ws:
                print(f"connected: {args.ws_url}")
                async for raw in ws:
                    frame_idx += 1
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict) or "links" not in payload:
                        continue

                    edges = frame_edges(payload, known_nodes)
                    if pending_edges is None or edges != pending_edges:
                        pending_edges = edges
                        pending_count = 1
                    else:
                        pending_count += 1

                    if pending_count < max(1, int(args.min_stable_frames)):
                        continue
                    if committed_edges is not None and edges == committed_edges:
                        continue

                    now = time.monotonic()
                    if committed_edges is not None and min_apply_interval_s > 0.0:
                        if now - last_apply_at < min_apply_interval_s:
                            continue

                    payloads = build_neighbor_payloads(edges=edges, entries=entries, frame_idx=frame_idx)
                    if args.dry_run:
                        print(f"frame={frame_idx} edges={len(edges)} nodes={len(entries)} neighbor_apply=dry-run")
                    else:
                        failures: list[str] = []
                        for entry in entries:
                            try:
                                neighbor_payload = json.loads(payloads[entry.container_exec])
                            except json.JSONDecodeError:
                                failures.append(f"{entry.node_id}({entry.container_exec}): invalid generated payload")
                                continue
                            ok, err = _write_neighbor_file(
                                entry.container_exec,
                                payloads[entry.container_exec],
                                str(args.neighbor_state_path),
                                timeout_s,
                            )
                            if not ok:
                                failures.append(f"{entry.node_id}({entry.container_exec}): {err}")
                                continue
                            ok, err = _sync_static_neighbors(
                                entry=entry,
                                desired_neighbors=list(neighbor_payload.get("neighbors") or []),
                                known_entries=entries,
                                neighbor_dev=str(args.neighbor_dev),
                                timeout_s=timeout_s,
                            )
                            if not ok:
                                failures.append(f"{entry.node_id}({entry.container_exec}): {err}")
                        if failures:
                            print(f"frame={frame_idx} edges={len(edges)} neighbor_apply=fail count={len(failures)}")
                            for line in failures[:5]:
                                print(f"  {line}")
                            continue
                        print(f"frame={frame_idx} edges={len(edges)} nodes={len(entries)} neighbor_apply=ok")

                    if args.state_output:
                        _persist_state_snapshot(
                            path=Path(args.state_output),
                            frame_idx=frame_idx,
                            edges=edges,
                            entries=entries,
                        )

                    committed_edges = edges
                    last_apply_at = time.monotonic()
                    if args.once:
                        return 0
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            print(f"warning: ws loop error: {exc}; reconnect in {args.reconnect_s}s", flush=True)
            await asyncio.sleep(float(args.reconnect_s))


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_controller(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
