#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import inspect
import json
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NodeEntry:
    node_id: str
    node_index: int
    container_name: str
    container_exec: str
    node_mac: str


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    rule_count: int
    error: str = ""


_MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")


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
        description="Subscribe dynamic-topo websocket frames and push L2 policy to the simulator container."
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
        "--sim-container",
        default="auto",
        help="Simulator container name/id, or auto to infer <prefix>_sim from mapping-csv",
    )
    parser.add_argument("--sim-policy-path", default="/opt/sim/policy.json", help="Policy JSON path in simulator")
    parser.add_argument(
        "--sim-proc-pattern",
        default="python3 /opt/sim/l2_center_sim.py",
        help="pgrep pattern used to locate simulator process for SIGHUP",
    )
    parser.add_argument("--node-mac-ifname", default="veth_0", help="Interface name used as node MAC source")
    parser.add_argument("--default-action", default="drop", help="Simulator default_action for no-match traffic")
    parser.add_argument("--min-stable-frames", type=int, default=2, help="Only apply when same edge set lasts N frames")
    parser.add_argument(
        "--min-apply-interval-s",
        type=float,
        default=0.0,
        help="Minimum seconds between successful policy applies after the initial apply",
    )
    parser.add_argument("--workers", type=int, default=16, help="Parallel workers for container MAC resolution")
    parser.add_argument("--reconnect-s", type=float, default=1.5, help="Reconnect delay after WS errors")
    parser.add_argument("--command-timeout-s", type=float, default=30.0, help="Timeout for each docker command")
    parser.add_argument(
        "--respect-proxy",
        action="store_true",
        help="Allow websockets client to use proxy env vars (default: disable proxy for WS)",
    )
    parser.add_argument(
        "--preserve-existing-extra",
        action="store_true",
        help="Preserve existing top-level fields in policy (except rules/default_action)",
    )
    parser.add_argument(
        "--output-policy",
        default="",
        help="Optional local path to write generated policy snapshot on each apply",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write simulator policy, print summary only")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process the first stable frame then exit (useful for one-shot apply)",
    )
    return parser.parse_args()


def _run_cmd(cmd: list[str], *, timeout_s: float, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


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


def _infer_sim_container_from_rows(rows: list[dict[str, str]]) -> list[str]:
    inferred: list[str] = []
    seen: set[str] = set()
    for row in rows:
        container_name = str(row.get("container_name", "")).strip()
        match = re.match(r"^(.*)_r_\d+$", container_name)
        if not match:
            continue
        sim_name = f"{match.group(1)}_sim"
        if sim_name in seen:
            continue
        seen.add(sim_name)
        inferred.append(sim_name)
    return inferred


def _inspect_one_container(ref: str, timeout_s: float) -> dict | None:
    proc = _run_cmd(["docker", "inspect", ref], timeout_s=timeout_s)
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


def resolve_sim_container(args: argparse.Namespace) -> str:
    requested = str(args.sim_container).strip()
    if requested and requested.lower() != "auto":
        return requested

    rows = _load_mapping_rows(Path(args.mapping_csv))
    timeout_s = float(args.command_timeout_s)

    candidates = _infer_sim_container_from_rows(rows) + ["star300lite_sim", "erv300_sim"]
    resolved: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if _inspect_one_container(candidate, timeout_s=timeout_s) is not None:
            resolved.append(candidate)

    if not resolved:
        raise ValueError(
            f"cannot resolve simulator container from mapping-csv={args.mapping_csv}; "
            "checked inferred *_sim plus star300lite_sim and erv300_sim"
        )
    return resolved[0]


def _extract_exec_mac(exec_target: str, ifname: str, timeout_s: float) -> str:
    proc = _run_cmd(
        ["docker", "exec", exec_target, "cat", f"/sys/class/net/{ifname}/address"],
        timeout_s=timeout_s,
    )
    if proc.returncode == 0:
        mac = (proc.stdout or "").strip().lower()
        if _MAC_RE.match(mac):
            return mac

    proc2 = _run_cmd(
        ["docker", "exec", exec_target, "ip", "-o", "link", "show", "dev", ifname],
        timeout_s=timeout_s,
    )
    if proc2.returncode != 0:
        return ""
    m = re.search(r"link/ether\s+([0-9a-f:]{17})", proc2.stdout.lower())
    if not m:
        return ""
    mac = m.group(1).strip().lower()
    return mac if _MAC_RE.match(mac) else ""


def _resolve_node_entry(
    row: dict[str, str],
    *,
    timeout_s: float,
    node_mac_ifname: str,
) -> tuple[NodeEntry | None, str, str]:
    node_id = str(row["node_id"]).strip()
    node_index = int(row["node_index"])
    container_name = str(row["container_name"]).strip()
    container_id = str(row.get("container_id", "")).strip()

    refs = []
    if container_name:
        refs.append(container_name)
    if container_id and container_id not in refs:
        refs.append(container_id)

    inspect_item: dict | None = None
    for ref in refs:
        inspect_item = _inspect_one_container(ref, timeout_s=timeout_s)
        if inspect_item is not None:
            break

    if inspect_item is None:
        return None, f"{node_id}({container_name}|{container_id})", ""

    actual_name = str(inspect_item.get("Name", "")).lstrip("/")
    short_id = str(inspect_item.get("Id", "")).strip()[:12]
    exec_target = actual_name or short_id or (refs[0] if refs else "")
    if not exec_target:
        return None, f"{node_id}({container_name}|{container_id})", ""

    mac = _extract_exec_mac(exec_target, node_mac_ifname, timeout_s=timeout_s)
    if not mac:
        return None, "", f"{node_id}({exec_target})"

    return (
        NodeEntry(
            node_id=node_id,
            node_index=node_index,
            container_name=container_name,
            container_exec=exec_target,
            node_mac=mac,
        ),
        "",
        "",
    )


def load_node_entries(args: argparse.Namespace) -> list[NodeEntry]:
    rows = _load_mapping_rows(Path(args.mapping_csv))
    rows.sort(key=lambda x: int(x["node_index"]))

    timeout_s = float(args.command_timeout_s)
    workers = max(1, int(args.workers))
    node_mac_ifname = str(args.node_mac_ifname)

    results: list[tuple[NodeEntry | None, str, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [
            pool.submit(
                _resolve_node_entry,
                row,
                timeout_s=timeout_s,
                node_mac_ifname=node_mac_ifname,
            )
            for row in rows
        ]
        for fut in futs:
            results.append(fut.result())

    entries: list[NodeEntry] = []
    missing_target: list[str] = []
    missing_mac: list[str] = []
    for entry, mt, mm in results:
        if mt:
            missing_target.append(mt)
            continue
        if mm:
            missing_mac.append(mm)
            continue
        if entry is not None:
            entries.append(entry)

    entries.sort(key=lambda x: x.node_index)

    if missing_target:
        details = ", ".join(missing_target[:8]) + (" ..." if len(missing_target) > 8 else "")
        raise ValueError(
            f"cannot resolve container reference for {len(missing_target)} nodes "
            f"(tried container_name/container_id), sample={details}"
        )
    if missing_mac:
        details = ", ".join(missing_mac[:8]) + (" ..." if len(missing_mac) > 8 else "")
        raise ValueError(
            f"missing node MAC for {len(missing_mac)} nodes on ifname={args.node_mac_ifname}, sample={details}"
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


def _generate_rules(
    edges: set[tuple[str, str]],
    by_node: dict[str, NodeEntry],
) -> list[dict[str, str]]:
    rules: list[dict[str, str]] = []
    for a, b in sorted(edges):
        ma = by_node[a].node_mac
        mb = by_node[b].node_mac
        if not ma or not mb or ma == mb:
            continue
        rules.append({"src_mac": ma, "dst_mac": mb, "pkt_type": "unicast", "action": "forward"})
        rules.append({"src_mac": mb, "dst_mac": ma, "pkt_type": "unicast", "action": "forward"})
    rules.sort(key=lambda x: (x["src_mac"], x["dst_mac"]))
    return rules


def _load_existing_policy_extra(args: argparse.Namespace) -> dict:
    cmd = [
        "docker",
        "exec",
        str(args.sim_container),
        "sh",
        "-lc",
        f"cat {shlex.quote(str(args.sim_policy_path))}",
    ]
    proc = _run_cmd(cmd, timeout_s=float(args.command_timeout_s))
    if proc.returncode != 0:
        return {}
    try:
        parsed = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out = {}
    for k, v in parsed.items():
        if k in ("rules", "default_action"):
            continue
        out[k] = v
    return out


def _build_policy(
    *,
    edges: set[tuple[str, str]],
    entries: list[NodeEntry],
    default_action: str,
    policy_extra: dict,
) -> dict:
    by_node = {e.node_id: e for e in entries}
    rules = _generate_rules(edges, by_node)
    policy = dict(policy_extra)
    if "version" not in policy:
        policy["version"] = "1.0"
    if "notes" not in policy:
        policy["notes"] = "Generated from dynamic-topo live links"
    policy["default_action"] = default_action
    policy["rules"] = rules
    return policy


def _write_and_reload_policy(args: argparse.Namespace, policy: dict) -> ApplyResult:
    rule_count = len(policy.get("rules", [])) if isinstance(policy.get("rules"), list) else 0
    payload = json.dumps(policy, ensure_ascii=False, indent=2, sort_keys=False) + "\n"

    if args.output_policy:
        Path(args.output_policy).write_text(payload, encoding="utf-8")

    if args.dry_run:
        return ApplyResult(ok=True, rule_count=rule_count)

    timeout_s = float(args.command_timeout_s)
    sim_container = str(args.sim_container)
    sim_policy_path = str(args.sim_policy_path)
    tmp_path = f"{sim_policy_path}.tmp"

    write_cmd = [
        "docker",
        "exec",
        "-i",
        sim_container,
        "sh",
        "-lc",
        f"cat > {shlex.quote(tmp_path)}",
    ]
    write_proc = _run_cmd(write_cmd, timeout_s=timeout_s, input_text=payload)
    if write_proc.returncode != 0:
        err = (write_proc.stderr or write_proc.stdout or "").strip()
        return ApplyResult(ok=False, rule_count=rule_count, error=f"write policy failed: {err[:800]}")

    hup_cmd = (
        "set -eu; "
        f"mv {shlex.quote(tmp_path)} {shlex.quote(sim_policy_path)}; "
        f"pid=$(pgrep -o -f {shlex.quote(str(args.sim_proc_pattern))} || true); "
        'if [ -z "$pid" ]; then echo "sim process not found" >&2; exit 1; fi; '
        'kill -HUP "$pid"'
    )
    hup_proc = _run_cmd(
        ["docker", "exec", sim_container, "sh", "-lc", hup_cmd],
        timeout_s=timeout_s,
    )
    if hup_proc.returncode != 0:
        err = (hup_proc.stderr or hup_proc.stdout or "").strip()
        return ApplyResult(ok=False, rule_count=rule_count, error=f"reload policy failed: {err[:800]}")
    return ApplyResult(ok=True, rule_count=rule_count)


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
    args.sim_container = resolve_sim_container(args)
    entries = load_node_entries(args)
    known_nodes = {e.node_id for e in entries}
    policy_extra = _load_existing_policy_extra(args) if args.preserve_existing_extra else {}

    print(
        f"loaded nodes={len(entries)} ws={args.ws_url} sim={args.sim_container}:{args.sim_policy_path} "
        f"dry_run={args.dry_run} min_stable_frames={args.min_stable_frames} "
        f"min_apply_interval_s={args.min_apply_interval_s}"
    )

    pending_edges: set[tuple[str, str]] | None = None
    pending_count = 0
    committed_edges: set[tuple[str, str]] | None = None
    frame_idx = 0
    last_apply_at = 0.0
    min_apply_interval_s = max(0.0, float(args.min_apply_interval_s))

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

                    policy = _build_policy(
                        edges=edges,
                        entries=entries,
                        default_action=str(args.default_action),
                        policy_extra=policy_extra,
                    )
                    res = _write_and_reload_policy(args, policy)
                    status = "ok" if res.ok else "fail"
                    print(
                        f"frame={frame_idx} edges={len(edges)} rules={res.rule_count} "
                        f"policy_apply={status}"
                    )
                    if not res.ok:
                        print(f"[error] {res.error}")

                    committed_edges = set(edges)
                    if res.ok:
                        last_apply_at = now
                    if args.once:
                        return 0 if res.ok else 2
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
