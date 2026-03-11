#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import inspect
import ipaddress
import json
import re
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class NodeEntry:
    node_id: str
    node_index: int
    container_name: str
    container_exec: str
    container_ip: str
    loopback_prefix: str


@dataclass(frozen=True)
class ApplyResult:
    container: str
    ok: bool
    upserts: int
    deletes: int
    error: str = ""


def resolve_ws_connect():
    try:
        from websockets.asyncio.client import connect as ws_connect  # type: ignore

        return ws_connect
    except Exception:
        try:
            from websockets import connect as ws_connect  # type: ignore

            return ws_connect
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "websockets package is required. Install project deps with `uv sync --dev` "
                "or `pip install websockets`."
            ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Subscribe dynamic-topo websocket frames and incrementally push static routes into containers."
    )
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8765", help="Dynamic topology websocket URL")
    parser.add_argument(
        "--mapping-csv",
        default="docs/node_mapping_300.csv",
        help=(
            "Node mapping CSV path. Required columns: node_id,node_index,container_name. "
            "Optional: container_id (used as fallback when container_name has changed)."
        ),
    )
    parser.add_argument(
        "--loopback-base-cidr",
        default="10.200.0.0/16",
        help="Base network for per-node destination prefixes",
    )
    parser.add_argument("--loopback-prefix-len", type=int, default=32, help="Prefix length for per-node destination")
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
    parser.add_argument("--route-dev", default="", help="Optional route device name (for ip route replace ... dev <if>)")
    parser.add_argument("--min-stable-frames", type=int, default=2, help="Only apply when same edge set lasts N frames")
    parser.add_argument(
        "--min-apply-interval-s",
        type=float,
        default=0.0,
        help="Minimum seconds between successful route applies after the initial apply",
    )
    parser.add_argument("--workers", type=int, default=8, help="Parallel container workers for route apply")
    parser.add_argument("--reconnect-s", type=float, default=1.5, help="Reconnect delay after WS errors")
    parser.add_argument("--command-timeout-s", type=float, default=30.0, help="Timeout for each docker exec")
    parser.add_argument(
        "--respect-proxy",
        action="store_true",
        help="Allow websockets client to use proxy env vars (default: disable proxy for WS)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands only, do not execute")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process the first stable frame then exit (useful for one-shot initial route install)",
    )
    return parser.parse_args()


def _normalize_csv_ip(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    ipaddress.ip_address(raw)
    return raw


def _split_fields(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def _assign_loopback_prefixes(
    rows: list[dict[str, str]],
    loopback_base_cidr: str,
    loopback_prefix_len: int,
) -> dict[str, str]:
    net = ipaddress.ip_network(loopback_base_cidr, strict=False)
    if net.version != 4:
        raise ValueError("only IPv4 loopback-base-cidr is supported")
    if loopback_prefix_len < net.prefixlen or loopback_prefix_len > 32:
        raise ValueError(
            f"loopback-prefix-len={loopback_prefix_len} must be between {net.prefixlen} and 32"
        )

    out: dict[str, str] = {}
    for row in rows:
        node_id = row["node_id"]
        idx = int(row["node_index"])
        host = ipaddress.ip_address(int(net.network_address) + idx)
        if host not in net:
            raise ValueError(
                f"node_index {idx} exceeds loopback-base-cidr {loopback_base_cidr}; cannot assign {node_id}"
            )
        out[node_id] = f"{host}/{loopback_prefix_len}"
    return out


def _load_mapping_rows(mapping_csv: Path) -> list[dict[str, str]]:
    with mapping_csv.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError(f"mapping csv is empty: {mapping_csv}")
    required = ("node_id", "node_index", "container_name")
    missing = [k for k in required if k not in rows[0]]
    if missing:
        raise ValueError(f"mapping csv missing required columns: {', '.join(missing)}")
    return rows


def _inspect_one_container(ref: str, timeout_s: float) -> dict | None:
    cmd = ["docker", "inspect", ref]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    item = data[0]
    if not isinstance(item, dict):
        return None
    return item


def _extract_container_ip(inspect_item: dict, docker_network: str) -> str:
    networks = inspect_item.get("NetworkSettings", {}).get("Networks", {}) or {}
    if not isinstance(networks, dict) or not networks:
        return ""
    if docker_network:
        info = networks.get(docker_network)
        if not isinstance(info, dict):
            return ""
        return str(info.get("IPAddress") or "").strip()

    for net_name in sorted(networks.keys()):
        info = networks.get(net_name)
        if not isinstance(info, dict):
            continue
        ip = str(info.get("IPAddress") or "").strip()
        if ip:
            return ip
    return ""


_IPV4_LINE_RE = re.compile(
    r"^\d+:\s+([^\s]+)\s+inet\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\/([0-9]+)\s+scope\s+([^\s]+)"
)


def _extract_exec_ipv4(ref: str, timeout_s: float) -> str:
    cmd = ["docker", "exec", ref, "ip", "-o", "-4", "addr", "show"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    if proc.returncode != 0:
        return ""

    candidates_primary: list[str] = []
    candidates_secondary: list[str] = []
    for raw in proc.stdout.splitlines():
        m = _IPV4_LINE_RE.match(raw.strip())
        if not m:
            continue
        ifname, ip, plen_text, scope = m.groups()
        if ifname == "lo":
            continue
        try:
            plen = int(plen_text)
        except ValueError:
            continue
        if scope != "global":
            continue
        # Prefer routable interface addresses over /32 host-loop style addresses.
        if plen < 32:
            candidates_primary.append(ip)
        else:
            candidates_secondary.append(ip)
    if candidates_primary:
        return candidates_primary[0]
    if candidates_secondary:
        return candidates_secondary[0]
    return ""


def _extract_exec_loopback_prefix(ref: str, timeout_s: float) -> str:
    cmd = ["docker", "exec", ref, "ip", "-o", "-4", "addr", "show", "dev", "lo"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    if proc.returncode != 0:
        return ""
    for raw in proc.stdout.splitlines():
        m = _IPV4_LINE_RE.match(raw.strip())
        if not m:
            continue
        ifname, ip, plen_text, scope = m.groups()
        if ifname != "lo":
            continue
        if ip.startswith("127."):
            continue
        if scope != "global":
            continue
        try:
            plen = int(plen_text)
        except ValueError:
            continue
        if plen != 32:
            continue
        return f"{ip}/32"
    return ""


def load_node_entries(args: argparse.Namespace) -> list[NodeEntry]:
    mapping_csv = Path(args.mapping_csv)
    rows = _load_mapping_rows(mapping_csv)
    rows.sort(key=lambda x: int(x["node_index"]))

    ip_fields = _split_fields(args.container_ip_fields)
    loopbacks = _assign_loopback_prefixes(rows, args.loopback_base_cidr, args.loopback_prefix_len)
    source = str(args.container_ip_source)
    docker_network = str(args.docker_network or "")
    timeout_s = float(args.command_timeout_s)

    entries: list[NodeEntry] = []
    missing_ip: list[str] = []
    missing_target: list[str] = []

    for row in rows:
        node_id = str(row["node_id"]).strip()
        node_index = int(row["node_index"])
        container_name = str(row["container_name"]).strip()
        container_id = str(row.get("container_id", "")).strip()

        csv_ip = ""
        if source in ("csv", "csv-or-docker"):
            for f in ip_fields:
                v = str(row.get(f, "")).strip()
                if not v:
                    continue
                csv_ip = _normalize_csv_ip(v)
                break

        inspect_item: dict | None = None
        inspect_ip = ""
        exec_target = ""

        if source in ("docker", "csv-or-docker"):
            refs = []
            if container_name:
                refs.append(container_name)
            if container_id and container_id not in refs:
                refs.append(container_id)

            for ref in refs:
                inspect_item = _inspect_one_container(ref, timeout_s=timeout_s)
                if inspect_item is not None:
                    break

            if inspect_item is not None:
                actual_name = str(inspect_item.get("Name", "")).lstrip("/")
                short_id = str(inspect_item.get("Id", "")).strip()[:12]
                exec_target = actual_name or short_id or (refs[0] if refs else "")
                inspect_ip = _extract_container_ip(inspect_item, docker_network=docker_network)
                if not inspect_ip and exec_target:
                    inspect_ip = _extract_exec_ipv4(exec_target, timeout_s=timeout_s)
            else:
                exec_target = container_id or container_name
                missing_target.append(f"{node_id}({container_name}|{container_id})")
        else:
            exec_target = container_id or container_name

        if source == "csv":
            container_ip = csv_ip
        elif source == "docker":
            container_ip = inspect_ip
        else:
            container_ip = csv_ip or inspect_ip

        runtime_loopback = ""
        if exec_target and source in ("docker", "csv-or-docker"):
            runtime_loopback = _extract_exec_loopback_prefix(exec_target, timeout_s=timeout_s)
        loopback_prefix = runtime_loopback or loopbacks[node_id]

        if not exec_target:
            missing_target.append(f"{node_id}({container_name}|{container_id})")
            continue
        if not container_ip:
            missing_ip.append(f"{node_id}({container_name}|{container_id})")
            continue

        entries.append(
            NodeEntry(
                node_id=node_id,
                node_index=node_index,
                container_name=container_name,
                container_exec=exec_target,
                container_ip=container_ip,
                loopback_prefix=loopback_prefix,
            )
        )

    if missing_target and source in ("docker", "csv-or-docker"):
        details = ", ".join(missing_target[:8]) + (" ..." if len(missing_target) > 8 else "")
        raise ValueError(
            f"cannot resolve container reference for {len(missing_target)} nodes "
            f"(tried container_name/container_id), sample={details}"
        )
    if missing_ip:
        details = ", ".join(missing_ip[:8]) + (" ..." if len(missing_ip) > 8 else "")
        raise ValueError(
            f"missing container_ip for {len(missing_ip)} nodes. "
            f"source={args.container_ip_source}, sample={details}"
        )
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


def _bfs_first_hop(src: str, neighbors: dict[str, set[str]]) -> dict[str, str]:
    first_hop: dict[str, str] = {}
    visited = {src}
    q: deque[str] = deque()
    for n in sorted(neighbors.get(src, set())):
        visited.add(n)
        first_hop[n] = n
        q.append(n)
    while q:
        u = q.popleft()
        for v in sorted(neighbors.get(u, set())):
            if v in visited:
                continue
            visited.add(v)
            first_hop[v] = first_hop[u]
            q.append(v)
    return first_hop


def desired_routes_from_edges(
    edges: set[tuple[str, str]],
    entries: list[NodeEntry],
) -> dict[str, dict[str, str]]:
    by_node: dict[str, NodeEntry] = {e.node_id: e for e in entries}
    neighbors: dict[str, set[str]] = {e.node_id: set() for e in entries}
    for a, b in edges:
        neighbors[a].add(b)
        neighbors[b].add(a)

    desired: dict[str, dict[str, str]] = {}
    node_ids = sorted(by_node.keys())
    for src in node_ids:
        first = _bfs_first_hop(src, neighbors)
        routes: dict[str, str] = {}
        for dst in node_ids:
            if dst == src:
                continue
            nh_node = first.get(dst)
            if not nh_node:
                continue
            dst_prefix = by_node[dst].loopback_prefix
            nh_ip = by_node[nh_node].container_ip
            routes[dst_prefix] = nh_ip
        desired[src] = routes
    return desired


def _render_route_cmd(prefix: str, nexthop_ip: str, route_dev: str) -> str:
    if route_dev:
        return f"ip -4 route replace {prefix} via {nexthop_ip} dev {route_dev}"
    return f"ip -4 route replace {prefix} via {nexthop_ip}"


def apply_container_diff(
    *,
    container: str,
    previous: dict[str, str],
    target: dict[str, str],
    route_dev: str,
    dry_run: bool,
    timeout_s: float,
) -> ApplyResult:
    to_delete = sorted(set(previous.keys()) - set(target.keys()))
    to_upsert = sorted((p, target[p]) for p in target.keys() if previous.get(p) != target[p])

    if not to_delete and not to_upsert:
        return ApplyResult(container=container, ok=True, upserts=0, deletes=0)

    if dry_run:
        for prefix in to_delete:
            print(f"[dry-run] docker exec {container} ip -4 route del {prefix}")
        for prefix, nh in to_upsert:
            print(f"[dry-run] docker exec {container} {_render_route_cmd(prefix, nh, route_dev)}")
        return ApplyResult(container=container, ok=True, upserts=len(to_upsert), deletes=len(to_delete))

    script_lines: list[str] = ["set -u", "rc=0"]
    for prefix in to_delete:
        script_lines.append(f"ip -4 route del {prefix} >/dev/null 2>&1 || true")
    for prefix, nh in to_upsert:
        script_lines.append(f"{_render_route_cmd(prefix, nh, route_dev)} || rc=1")
    script_lines.append("exit $rc")
    script = "\n".join(script_lines) + "\n"

    cmd = ["docker", "exec", "-i", container, "sh"]
    proc = subprocess.run(
        cmd,
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    ok = proc.returncode == 0
    error = ""
    if not ok:
        err = (proc.stderr or proc.stdout or "").strip()
        error = err[:800]
    return ApplyResult(
        container=container,
        ok=ok,
        upserts=len(to_upsert),
        deletes=len(to_delete),
        error=error,
    )


def apply_routes_incremental(
    *,
    desired_by_node: dict[str, dict[str, str]],
    entries: list[NodeEntry],
    applied_by_container: dict[str, dict[str, str]],
    route_dev: str,
    dry_run: bool,
    timeout_s: float,
    workers: int,
) -> tuple[dict[str, dict[str, str]], list[ApplyResult]]:
    by_node = {e.node_id: e for e in entries}
    futures = []
    results: list[ApplyResult] = []
    next_state: dict[str, dict[str, str]] = {k: dict(v) for k, v in applied_by_container.items()}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for node_id, target in desired_by_node.items():
            entry = by_node[node_id]
            container = entry.container_exec
            prev = applied_by_container.get(container, {})
            fut = pool.submit(
                apply_container_diff,
                container=container,
                previous=prev,
                target=target,
                route_dev=route_dev,
                dry_run=dry_run,
                timeout_s=timeout_s,
            )
            futures.append((container, target, fut))

        for container, target, fut in futures:
            res: ApplyResult = fut.result()
            results.append(res)
            if res.ok:
                next_state[container] = dict(target)

    return next_state, results


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
    print(
        f"loaded nodes={len(entries)} ws={args.ws_url} source={args.container_ip_source} "
        f"dry_run={args.dry_run} min_stable_frames={args.min_stable_frames} "
        f"min_apply_interval_s={args.min_apply_interval_s}"
    )

    ws_kwargs = _ws_connect_kwargs(ws_connect, respect_proxy=bool(args.respect_proxy))
    if "proxy" in ws_kwargs and ws_kwargs["proxy"] is None:
        print("ws proxy disabled (pass --respect-proxy to override)")

    pending_edges: set[tuple[str, str]] | None = None
    pending_count = 0
    committed_edges: set[tuple[str, str]] | None = None
    applied_by_container: dict[str, dict[str, str]] = {}
    frame_idx = 0
    last_apply_at = 0.0
    min_apply_interval_s = max(0.0, float(args.min_apply_interval_s))

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
                    if isinstance(payload, dict) and payload.get("type") == "control_ack":
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if "links" not in payload:
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

                    desired = desired_routes_from_edges(edges, entries)
                    applied_by_container, results = apply_routes_incremental(
                        desired_by_node=desired,
                        entries=entries,
                        applied_by_container=applied_by_container,
                        route_dev=str(args.route_dev or ""),
                        dry_run=bool(args.dry_run),
                        timeout_s=float(args.command_timeout_s),
                        workers=int(args.workers),
                    )

                    ok = sum(1 for r in results if r.ok)
                    bad = len(results) - ok
                    upserts = sum(r.upserts for r in results)
                    deletes = sum(r.deletes for r in results)
                    print(
                        f"frame={frame_idx} edges={len(edges)} apply_ok={ok} apply_fail={bad} "
                        f"route_upserts={upserts} route_deletes={deletes}"
                    )
                    for res in results:
                        if not res.ok:
                            print(f"[error] container={res.container} msg={res.error}")

                    committed_edges = set(edges)
                    if bad == 0:
                        last_apply_at = now
                    if args.once:
                        return 0 if bad == 0 else 2
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"[ws-error] {exc}; reconnect in {args.reconnect_s}s", file=sys.stderr)
            await asyncio.sleep(float(args.reconnect_s))


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_controller(args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
