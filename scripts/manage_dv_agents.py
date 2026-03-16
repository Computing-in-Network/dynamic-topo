#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NodeEntry:
    node_id: str
    node_index: int
    container_exec: str
    loopback_prefix: str


def _agent_pattern(remote: str) -> str:
    return f"python3 /opt/[d]v/{Path(remote).name}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install/start/stop the Route A DV agent inside node containers.")
    parser.add_argument(
        "--action",
        required=True,
        choices=("install-start", "stop", "status"),
        help="Agent management action",
    )
    parser.add_argument("--mapping-csv", default="docs/node_mapping_300.csv", help="Node mapping CSV path")
    parser.add_argument("--max-nodes", type=int, default=10, help="How many nodes from mapping CSV to target")
    parser.add_argument("--workers", type=int, default=8, help="Parallel container workers")
    parser.add_argument("--agent-local-path", default="scripts/dv_agent.py", help="Local dv_agent.py path")
    parser.add_argument("--agent-remote-path", default="/opt/dv/dv_agent.py", help="Remote path inside containers")
    parser.add_argument("--neighbor-state-path", default="/run/dv/neighbors.json", help="Neighbor-state JSON path")
    parser.add_argument("--state-output-path", default="/run/dv/routes.json", help="Agent state snapshot path")
    parser.add_argument("--listen-port", type=int, default=5510, help="UDP listen port used by dv_agent")
    parser.add_argument("--update-interval-s", type=float, default=5.0, help="DV periodic update interval")
    parser.add_argument("--route-timeout-s", type=float, default=15.0, help="DV route timeout interval")
    parser.add_argument("--route-dev", default="veth_0", help="Linux route device for dv_agent")
    parser.add_argument("--command-timeout-s", type=float, default=30.0, help="Timeout for each docker command")
    return parser.parse_args()


def _run_cmd(cmd: list[str], timeout_s: float, *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def _load_mapping_rows(path: Path, max_nodes: int) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError(f"mapping csv is empty: {path}")
    rows.sort(key=lambda x: int(x["node_index"]))
    return rows[: max(1, int(max_nodes))]


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


def _resolve_entry(row: dict[str, str], timeout_s: float) -> NodeEntry:
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
        raise ValueError(f"cannot resolve container exec target for {row['node_id']}")
    loopback_prefix = _extract_loopback_prefix(exec_target, timeout_s=timeout_s)
    if not loopback_prefix:
        raise ValueError(f"missing loopback prefix for {row['node_id']}({exec_target})")
    return NodeEntry(
        node_id=str(row["node_id"]),
        node_index=int(row["node_index"]),
        container_exec=exec_target,
        loopback_prefix=loopback_prefix,
    )


def load_entries(args: argparse.Namespace) -> list[NodeEntry]:
    rows = _load_mapping_rows(Path(args.mapping_csv), max_nodes=int(args.max_nodes))
    timeout_s = float(args.command_timeout_s)
    workers = max(1, int(args.workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_resolve_entry, row, timeout_s) for row in rows]
        entries = [f.result() for f in futs]
    entries.sort(key=lambda x: x.node_index)
    return entries


def _flush_subset_routes(entry: NodeEntry, all_prefixes: list[str], args: argparse.Namespace) -> tuple[bool, str]:
    prefixes = [p for p in all_prefixes if p != entry.loopback_prefix]
    if not prefixes:
        return True, ""
    shell = "set -eu; " + " ".join(
        [f"ip -4 route del {prefix} >/dev/null 2>&1 || true;" for prefix in prefixes]
    )
    proc = _run_cmd(["docker", "exec", entry.container_exec, "sh", "-lc", shell], timeout_s=float(args.command_timeout_s))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err[:400]
    return True, ""


def _install_agent_script(entry: NodeEntry, args: argparse.Namespace, script_payload: str) -> tuple[bool, str]:
    remote = str(args.agent_remote_path)
    mkdir_cmd = ["docker", "exec", entry.container_exec, "sh", "-lc", f"mkdir -p {Path(remote).parent.as_posix()} /run/dv"]
    proc = _run_cmd(mkdir_cmd, timeout_s=float(args.command_timeout_s))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, f"mkdir failed: {err[:300]}"

    proc = _run_cmd(
        ["docker", "exec", "-i", entry.container_exec, "sh", "-lc", f"cat > {remote}"],
        timeout_s=float(args.command_timeout_s),
        input_text=script_payload,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, f"copy failed: {err[:300]}"

    proc = _run_cmd(["docker", "exec", entry.container_exec, "chmod", "+x", remote], timeout_s=float(args.command_timeout_s))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, f"chmod failed: {err[:300]}"
    return True, ""


def _stop_agent(entry: NodeEntry, args: argparse.Namespace) -> tuple[bool, str]:
    remote = str(args.agent_remote_path)
    pattern = _agent_pattern(remote)
    shell = f"pkill -f {pattern!r} >/dev/null 2>&1 || true"
    proc = _run_cmd(["docker", "exec", entry.container_exec, "sh", "-lc", shell], timeout_s=float(args.command_timeout_s))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err[:300]
    return True, ""


def _start_agent(entry: NodeEntry, args: argparse.Namespace) -> tuple[bool, str]:
    remote = str(args.agent_remote_path)
    pattern = _agent_pattern(remote)
    neighbor_path = str(args.neighbor_state_path)
    state_path = str(args.state_output_path)
    log_path = "/tmp/dv_agent.log"
    shell = (
        "set -eu; "
        f"pkill -f {pattern!r} >/dev/null 2>&1 || true; "
        f": > {log_path}; "
        f"nohup python3 {remote} "
        f"--node-id {entry.node_id} "
        f"--local-prefix {entry.loopback_prefix} "
        f"--listen-port {int(args.listen_port)} "
        f"--route-dev {args.route_dev} "
        f"--neighbor-state {neighbor_path} "
        f"--state-output {state_path} "
        f"--update-interval-s {float(args.update_interval_s)} "
        f"--route-timeout-s {float(args.route_timeout_s)} "
        f"> {log_path} 2>&1 & "
        f"sleep 1; pgrep -af {pattern!r}"
    )
    proc = _run_cmd(["docker", "exec", entry.container_exec, "sh", "-lc", shell], timeout_s=float(args.command_timeout_s))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err[:400]
    return True, (proc.stdout or "").strip()


def _status_agent(entry: NodeEntry, args: argparse.Namespace) -> tuple[bool, str]:
    remote = str(args.agent_remote_path)
    pattern = _agent_pattern(remote)
    proc = _run_cmd(
        ["docker", "exec", entry.container_exec, "sh", "-lc", f"pgrep -af {pattern!r} || true"],
        timeout_s=float(args.command_timeout_s),
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err[:300]
    return True, (proc.stdout or "").strip() or "stopped"


def main() -> int:
    args = parse_args()
    entries = load_entries(args)
    all_prefixes = [entry.loopback_prefix for entry in entries]
    workers = max(1, int(args.workers))

    if args.action == "install-start":
        script_payload = Path(args.agent_local_path).read_text(encoding="utf-8")
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for fut in [pool.submit(_flush_subset_routes, entry, all_prefixes, args) for entry in entries]:
                ok, msg = fut.result()
                if not ok:
                    failures.append(f"flush failed: {msg}")
            for fut in [pool.submit(_install_agent_script, entry, args, script_payload) for entry in entries]:
                ok, msg = fut.result()
                if not ok:
                    failures.append(f"install failed: {msg}")
            start_futs = {pool.submit(_start_agent, entry, args): entry for entry in entries}
            for fut, entry in start_futs.items():
                ok, msg = fut.result()
                if ok:
                    print(f"{entry.node_id} start_ok {msg}")
                else:
                    failures.append(f"{entry.node_id} start failed: {msg}")
        if failures:
            for line in failures:
                print(line, file=sys.stderr)
            return 1
        print(f"install_start_ok nodes={len(entries)}")
        return 0

    if args.action == "stop":
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            stop_futs = {pool.submit(_stop_agent, entry, args): entry for entry in entries}
            for fut, entry in stop_futs.items():
                ok, msg = fut.result()
                if not ok:
                    failures.append(f"{entry.node_id} stop failed: {msg}")
            flush_futs = {pool.submit(_flush_subset_routes, entry, all_prefixes, args): entry for entry in entries}
            for fut, entry in flush_futs.items():
                ok, msg = fut.result()
                if not ok:
                    failures.append(f"{entry.node_id} flush failed: {msg}")
        if failures:
            for line in failures:
                print(line, file=sys.stderr)
            return 1
        print(f"stop_ok nodes={len(entries)}")
        return 0

    if args.action == "status":
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_status_agent, entry, args): entry for entry in entries}
            for fut, entry in futs.items():
                ok, msg = fut.result()
                status = msg if ok else f"error={msg}"
                print(f"{entry.node_id} {entry.container_exec} {status}")
        return 0

    raise SystemExit(f"unsupported action: {args.action}")


if __name__ == "__main__":
    raise SystemExit(main())
