#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore simulator veth links and optionally restart l2_center_sim.py."
    )
    parser.add_argument("--sim-container", default="star300lite_sim", help="Simulator container name or id")
    parser.add_argument(
        "--helper-image",
        default="ubuntu/sim-node-lite:v1",
        help="Local helper image used for privileged host-side recovery",
    )
    parser.add_argument("--host-in-if", default="s3l_sinh", help="Host-side ingress interface expected by OVS")
    parser.add_argument("--host-out-if", default="s3l_south", help="Host-side egress interface expected by OVS")
    parser.add_argument("--container-in-if", default="sim_in", help="Container-side ingress interface")
    parser.add_argument("--container-out-if", default="sim_out", help="Container-side egress interface")
    parser.add_argument("--sim-script", default="/opt/sim/l2_center_sim.py", help="Simulator python entrypoint")
    parser.add_argument("--sim-policy-path", default="/opt/sim/policy.json", help="Simulator policy path")
    parser.add_argument(
        "--sim-proc-pattern",
        default="python3 /opt/sim/l2_center_sim.py",
        help="Exact process command prefix used when restarting simulator",
    )
    parser.add_argument(
        "--restart-sim",
        action="store_true",
        help="Restart l2_center_sim.py after restoring interfaces",
    )
    parser.add_argument(
        "--check-ovs",
        action="store_true",
        help="Verify that host-side interfaces are bound to existing OVS interface records",
    )
    parser.add_argument("--timeout-s", type=float, default=60.0, help="Timeout for each shell command")
    return parser.parse_args()


def run_cmd(cmd: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def docker_inspect(ref: str, *, timeout_s: float) -> dict:
    proc = run_cmd(["docker", "inspect", ref], timeout_s=timeout_s)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"docker inspect failed for {ref}: {err[:400]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"docker inspect returned invalid JSON for {ref}") from exc
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RuntimeError(f"docker inspect returned unexpected payload for {ref}")
    return payload[0]


def require_running_container(ref: str, *, timeout_s: float) -> tuple[str, int]:
    item = docker_inspect(ref, timeout_s=timeout_s)
    actual_name = str(item.get("Name", "")).lstrip("/") or ref
    state = item.get("State", {}) or {}
    if not isinstance(state, dict) or not state.get("Running"):
        raise RuntimeError(f"container is not running: {actual_name}")
    pid = int(state.get("Pid") or 0)
    if pid <= 0:
        raise RuntimeError(f"container has invalid pid: {actual_name}")
    return actual_name, pid


def restore_pair_script(*, sim_pid: int, host_if: str, container_if: str) -> str:
    host_q = shlex.quote(host_if)
    cont_q = shlex.quote(container_if)
    return (
        f"if ! nsenter -t {sim_pid} -n ip link show {cont_q} >/dev/null 2>&1; then "
        f"  ip link show {host_q} >/dev/null 2>&1 && ip link delete {host_q} >/dev/null 2>&1 || true; "
        f"  ip link add {host_q} type veth peer name {cont_q}; "
        f"  ip link set {cont_q} netns {sim_pid}; "
        "fi; "
        f"ip link set {host_q} up; "
        f"nsenter -t {sim_pid} -n ip link set {cont_q} up; "
        f"echo '[host]'; ip -o link show {host_q}; "
        f"echo '[container]'; nsenter -t {sim_pid} -n ip -o link show {cont_q}; "
    )


def restore_datapath(args: argparse.Namespace, *, sim_pid: int) -> None:
    host_script = "set -euo pipefail; "
    host_script += restore_pair_script(sim_pid=sim_pid, host_if=args.host_in_if, container_if=args.container_in_if)
    host_script += restore_pair_script(sim_pid=sim_pid, host_if=args.host_out_if, container_if=args.container_out_if)

    cmd = [
        "docker",
        "run",
        "--rm",
        "--privileged",
        "--network",
        "host",
        "--pid",
        "host",
        "-v",
        "/:/host",
        args.helper_image,
        "chroot",
        "/host",
        "/bin/bash",
        "-lc",
        host_script,
    ]
    proc = run_cmd(cmd, timeout_s=float(args.timeout_s))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"restore datapath failed: {err[:800]}")
    if proc.stdout.strip():
        print(proc.stdout.strip())


def check_ovs_binding(args: argparse.Namespace) -> None:
    checks = []
    for ifname in (args.host_in_if, args.host_out_if):
        checks.append(
            f"/usr/local/bin/ovs-vsctl --columns=name,ofport,ofport_request,error "
            f"find interface name={shlex.quote(ifname)}"
        )
    cmd = [
        "docker",
        "run",
        "--rm",
        "--privileged",
        "-v",
        "/:/host",
        args.helper_image,
        "chroot",
        "/host",
        "/bin/bash",
        "-lc",
        "set -euo pipefail; " + "; ".join(checks),
    ]
    proc = run_cmd(cmd, timeout_s=float(args.timeout_s))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ovs verification failed: {err[:800]}")
    if proc.stdout.strip():
        print(proc.stdout.strip())


def restart_simulator(args: argparse.Namespace, *, sim_container: str) -> None:
    quoted_script = shlex.quote(args.sim_script)
    quoted_policy = shlex.quote(args.sim_policy_path)
    quoted_in_if = shlex.quote(args.container_in_if)
    quoted_out_if = shlex.quote(args.container_out_if)
    quoted_pattern = shlex.quote(f"^{args.sim_proc_pattern}")
    shell = (
        "set -eu; "
        f"pkill -f {quoted_pattern} >/dev/null 2>&1 || true; "
        ": >/tmp/sim.log; "
        f"nohup python3 {quoted_script} --in-if {quoted_in_if} --out-if {quoted_out_if} "
        f"--policy {quoted_policy} >/tmp/sim.log 2>&1 & "
        "sleep 1; "
        f"pgrep -af {quoted_pattern}; "
        "tr -d '\\000' </tmp/sim.log | tail -n 20"
    )
    proc = run_cmd(["docker", "exec", sim_container, "sh", "-lc", shell], timeout_s=float(args.timeout_s))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"restart simulator failed: {err[:800]}")
    if proc.stdout.strip():
        print(proc.stdout.strip())


def main() -> int:
    args = parse_args()
    try:
        sim_container, sim_pid = require_running_container(args.sim_container, timeout_s=float(args.timeout_s))
        print(f"sim_container={sim_container} pid={sim_pid}")
        restore_datapath(args, sim_pid=sim_pid)
        if args.check_ovs:
            check_ovs_binding(args)
        if args.restart_sim:
            restart_simulator(args, sim_container=sim_container)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
