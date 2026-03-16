#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import select
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class NeighborInfo:
    node_id: str
    ip: str
    prefix: str


@dataclass
class RouteEntry:
    prefix: str
    metric: int
    next_hop_ip: str
    learned_from: str
    kind: str
    updated_at: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal distance-vector agent for star300lite Route A prototype.")
    parser.add_argument("--node-id", required=True, help="Current node id, e.g. SAT-POLAR-001")
    parser.add_argument("--local-prefix", required=True, help="Current node loopback prefix, e.g. 10.255.0.1/32")
    parser.add_argument("--listen-host", default="0.0.0.0", help="Local bind host for DV UDP messages")
    parser.add_argument("--listen-port", type=int, default=5510, help="Local bind port for DV UDP messages")
    parser.add_argument("--route-dev", default="veth_0", help="Route device used for ip route replace")
    parser.add_argument("--neighbor-state", default="/run/dv/neighbors.json", help="Neighbor JSON pushed by controller")
    parser.add_argument("--state-output", default="/run/dv/routes.json", help="Local DV state snapshot JSON")
    parser.add_argument("--update-interval-s", type=float, default=5.0, help="Periodic full-vector interval")
    parser.add_argument("--neighbor-poll-s", type=float, default=1.0, help="How often to reload neighbor JSON")
    parser.add_argument("--route-timeout-s", type=float, default=15.0, help="Timeout for learned DV routes")
    parser.add_argument("--metric-infinity", type=int, default=16, help="Infinity metric for poison reverse")
    parser.add_argument("--command-timeout-s", type=float, default=5.0, help="Timeout for ip route commands")
    return parser.parse_args()


class DVAgent:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.node_id = str(args.node_id)
        self.local_prefix = self._normalize_prefix(args.local_prefix)
        self.local_ip = str(ipaddress.ip_interface(args.local_prefix).ip)
        self.route_dev = str(args.route_dev)
        self.neighbor_state_path = Path(args.neighbor_state)
        self.state_output_path = Path(args.state_output)
        self.update_interval_s = max(0.5, float(args.update_interval_s))
        self.neighbor_poll_s = max(0.2, float(args.neighbor_poll_s))
        self.route_timeout_s = max(self.update_interval_s, float(args.route_timeout_s))
        self.metric_infinity = max(2, int(args.metric_infinity))
        self.command_timeout_s = max(1.0, float(args.command_timeout_s))

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((str(args.listen_host), int(args.listen_port)))
        self.sock.setblocking(False)

        now = time.time()
        self.routes: dict[str, RouteEntry] = {
            self.local_prefix: RouteEntry(
                prefix=self.local_prefix,
                metric=0,
                next_hop_ip="",
                learned_from=self.node_id,
                kind="self",
                updated_at=now,
            )
        }
        self.applied_routes: dict[str, str] = {}
        self.neighbors: dict[str, NeighborInfo] = {}
        self.neighbor_file_mtime_ns: int | None = None
        self.seq = 0
        self.last_periodic_send = 0.0
        self.last_neighbor_poll = 0.0
        self.dirty = True

    def log(self, msg: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"[{stamp}] [{self.node_id}] {msg}", flush=True)

    def _normalize_prefix(self, raw: str) -> str:
        return str(ipaddress.ip_interface(raw).network)

    def _normalize_ip(self, raw: str) -> str:
        return str(ipaddress.ip_address(raw.strip()))

    def _read_neighbor_state(self) -> tuple[dict[str, NeighborInfo], str]:
        if not self.neighbor_state_path.exists():
            return {}, ""
        try:
            raw = json.loads(self.neighbor_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.log(f"ignore invalid neighbor json: {exc}")
            return self.neighbors, ""
        if not isinstance(raw, dict):
            return self.neighbors, ""

        local_prefix = self._normalize_prefix(str(raw.get("local_prefix") or self.local_prefix))
        loaded: dict[str, NeighborInfo] = {}
        for item in raw.get("neighbors", []):
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("node_id") or "").strip()
            ip = str(item.get("ip") or "").strip()
            prefix = str(item.get("prefix") or "").strip()
            if not node_id or not ip or not prefix:
                continue
            try:
                loaded[node_id] = NeighborInfo(
                    node_id=node_id,
                    ip=self._normalize_ip(ip),
                    prefix=self._normalize_prefix(prefix),
                )
            except ValueError:
                continue
        return loaded, local_prefix

    def _reconcile_neighbors(self, now: float) -> None:
        try:
            stat = self.neighbor_state_path.stat()
        except FileNotFoundError:
            stat = None

        if stat is not None and self.neighbor_file_mtime_ns == stat.st_mtime_ns:
            return
        self.neighbor_file_mtime_ns = None if stat is None else stat.st_mtime_ns

        new_neighbors, local_prefix = self._read_neighbor_state()
        if local_prefix != self.local_prefix:
            self.log(f"neighbor file local_prefix mismatch: file={local_prefix} self={self.local_prefix}")

        removed = set(self.neighbors) - set(new_neighbors)
        added_or_changed = {
            key
            for key, value in new_neighbors.items()
            if key not in self.neighbors or self.neighbors[key] != value
        }

        if not removed and not added_or_changed:
            return

        self.neighbors = new_neighbors

        for node_id in removed:
            self._withdraw_neighbor(node_id)

        for node_id in sorted(added_or_changed):
            nbr = self.neighbors[node_id]
            self.routes[nbr.prefix] = RouteEntry(
                prefix=nbr.prefix,
                metric=1,
                next_hop_ip=nbr.ip,
                learned_from=nbr.node_id,
                kind="direct",
                updated_at=now,
            )

        self.dirty = True
        self._sync_kernel_routes()
        self._write_state(now)
        self.log(
            "neighbors updated: "
            f"count={len(self.neighbors)} added_or_changed={len(added_or_changed)} removed={len(removed)}"
        )

    def _withdraw_neighbor(self, node_id: str) -> None:
        to_delete = [
            prefix
            for prefix, route in self.routes.items()
            if route.kind != "self" and route.learned_from == node_id
        ]
        for prefix in to_delete:
            self.routes.pop(prefix, None)

    def _export_routes_for_neighbor(self, neighbor_node_id: str) -> list[dict[str, int | str]]:
        exported: list[dict[str, int | str]] = []
        for prefix, route in sorted(self.routes.items()):
            metric = int(route.metric)
            if route.kind != "self" and route.learned_from == neighbor_node_id:
                metric = self.metric_infinity
            exported.append({"prefix": prefix, "metric": metric})
        return exported

    def _send_update(self, *, reason: str, now: float) -> None:
        for neighbor in self.neighbors.values():
            payload = {
                "type": "dv_update",
                "schema": "dynamic_topo.dv_update.v1",
                "node_id": self.node_id,
                "local_prefix": self.local_prefix,
                "seq": self.seq,
                "routes": self._export_routes_for_neighbor(neighbor.node_id),
                "sent_at": now,
            }
            wire = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            self.sock.sendto(wire, (neighbor.ip, int(self.args.listen_port)))
        self.seq += 1
        self.last_periodic_send = now
        self.log(f"update_sent reason={reason} neighbors={len(self.neighbors)} routes={len(self.routes)} seq={self.seq}")

    def _handle_update(self, payload: dict, src_ip: str, now: float) -> None:
        msg_type = str(payload.get("type") or "")
        src_node_id = str(payload.get("node_id") or "").strip()
        if msg_type != "dv_update" or not src_node_id or src_node_id == self.node_id:
            return

        neighbor = self.neighbors.get(src_node_id)
        if neighbor is None:
            for item in self.neighbors.values():
                if item.ip == src_ip:
                    neighbor = item
                    break
        if neighbor is None:
            return

        changed = False
        for item in payload.get("routes", []):
            if not isinstance(item, dict):
                continue
            prefix_raw = str(item.get("prefix") or "").strip()
            if not prefix_raw:
                continue
            try:
                prefix = self._normalize_prefix(prefix_raw)
            except ValueError:
                continue
            if prefix == self.local_prefix:
                continue
            try:
                metric = int(item.get("metric"))
            except Exception:
                continue
            advertised = max(0, min(metric, self.metric_infinity))
            if prefix == neighbor.prefix:
                advertised = 0

            current = self.routes.get(prefix)
            if advertised >= self.metric_infinity:
                if current is not None and current.kind == "dv" and current.learned_from == neighbor.node_id:
                    self.routes.pop(prefix, None)
                    changed = True
                continue

            candidate_metric = min(self.metric_infinity, advertised + 1)
            if prefix == neighbor.prefix:
                candidate_metric = 1

            if current is not None and current.kind == "direct" and current.learned_from != neighbor.node_id:
                continue

            if (
                current is None
                or (current.kind == "dv" and current.learned_from == neighbor.node_id)
                or candidate_metric < current.metric
            ):
                next_kind = "direct" if prefix == neighbor.prefix else "dv"
                self.routes[prefix] = RouteEntry(
                    prefix=prefix,
                    metric=candidate_metric,
                    next_hop_ip=neighbor.ip,
                    learned_from=neighbor.node_id,
                    kind=next_kind,
                    updated_at=now,
                )
                changed = True

        if changed:
            self.dirty = True
            self._sync_kernel_routes()
            self._write_state(now)

    def _expire_routes(self, now: float) -> None:
        expired = [
            prefix
            for prefix, route in self.routes.items()
            if route.kind == "dv" and (now - route.updated_at) >= self.route_timeout_s
        ]
        if not expired:
            return
        for prefix in expired:
            self.routes.pop(prefix, None)
        self.dirty = True
        self._sync_kernel_routes()
        self._write_state(now)
        self.log(f"expired_routes count={len(expired)}")

    def _desired_kernel_routes(self) -> dict[str, str]:
        desired: dict[str, str] = {}
        for prefix, route in self.routes.items():
            if route.kind == "self":
                continue
            if route.metric >= self.metric_infinity or not route.next_hop_ip:
                continue
            desired[prefix] = route.next_hop_ip
        return desired

    def _run_route_cmd(self, cmd: list[str]) -> bool:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.command_timeout_s,
            check=False,
        )
        if proc.returncode == 0:
            return True
        err = (proc.stderr or proc.stdout or "").strip()
        self.log(f"route_cmd_failed cmd={' '.join(cmd)} err={err[:300]}")
        return False

    def _sync_kernel_routes(self) -> None:
        desired = self._desired_kernel_routes()
        stale = sorted(set(self.applied_routes) - set(desired))
        for prefix in stale:
            self._run_route_cmd(["ip", "-4", "route", "del", prefix])
            self.applied_routes.pop(prefix, None)

        for prefix, next_hop in sorted(desired.items()):
            current = self.applied_routes.get(prefix)
            if current == next_hop:
                continue
            ok = self._run_route_cmd(
                ["ip", "-4", "route", "replace", prefix, "via", next_hop, "dev", self.route_dev, "src", self.local_ip]
            )
            if ok:
                self.applied_routes[prefix] = next_hop

    def _write_state(self, now: float) -> None:
        self.state_output_path.parent.mkdir(parents=True, exist_ok=True)
        routes = []
        for prefix, route in sorted(self.routes.items()):
            item = asdict(route)
            item["age_s"] = max(0.0, now - route.updated_at)
            routes.append(item)
        payload = {
            "schema": "dynamic_topo.dv_agent_state.v1",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "node_id": self.node_id,
            "local_prefix": self.local_prefix,
            "neighbors": [asdict(x) for _, x in sorted(self.neighbors.items())],
            "routes": routes,
            "applied_routes": self.applied_routes,
        }
        self.state_output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def loop(self) -> int:
        self.log(
            f"agent_start local_prefix={self.local_prefix} listen={self.args.listen_host}:{self.args.listen_port} "
            f"neighbor_state={self.neighbor_state_path}"
        )
        now = time.time()
        self._reconcile_neighbors(now)
        self._sync_kernel_routes()
        self._write_state(now)

        while True:
            now = time.time()
            if now - self.last_neighbor_poll >= self.neighbor_poll_s:
                self.last_neighbor_poll = now
                self._reconcile_neighbors(now)
            self._expire_routes(now)

            if self.dirty:
                self.dirty = False
                self._send_update(reason="triggered", now=now)
            elif now - self.last_periodic_send >= self.update_interval_s:
                self._send_update(reason="periodic", now=now)

            timeout = min(0.5, self.neighbor_poll_s)
            readable, _, _ = select.select([self.sock], [], [], timeout)
            if not readable:
                continue
            try:
                data, addr = self.sock.recvfrom(65535)
            except BlockingIOError:
                continue
            except OSError as exc:
                self.log(f"recv_failed err={exc}")
                continue
            try:
                payload = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            self._handle_update(payload, str(addr[0]), time.time())


def main() -> int:
    args = parse_args()
    agent = DVAgent(args)
    try:
        return agent.loop()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
