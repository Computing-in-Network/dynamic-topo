#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MappingRow:
    node_id: str
    node_index: int
    container_name: str
    container_id: str
    container_status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate node mapping CSV for the 300-node topology from a container name prefix."
    )
    parser.add_argument("--container-prefix", default="star300lite_r_", help="Container prefix, e.g. star300lite_r_")
    parser.add_argument("--count", type=int, default=300, help="Expected node count")
    parser.add_argument("--output", default="docs/node_mapping_300.csv", help="Output CSV path")
    parser.add_argument("--workers", type=int, default=16, help="Parallel workers for docker inspect")
    return parser.parse_args()


def node_id_from_index(index: int) -> str:
    if 1 <= index <= 100:
        return f"SAT-POLAR-{index:03d}"
    if 101 <= index <= 200:
        return f"SAT-INCL-{index - 100:03d}"
    if 201 <= index <= 250:
        return f"AIR-{index - 200:03d}"
    if 251 <= index <= 300:
        return f"SHIP-{index - 250:03d}"
    raise ValueError(f"unsupported node index: {index}")


def inspect_container(name: str) -> tuple[str, str]:
    proc = subprocess.run(
        ["docker", "inspect", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return "", "missing"
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return "", "inspect-error"
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return "", "inspect-error"

    item = payload[0]
    container_id = str(item.get("Id", "")).strip()[:12]
    state = item.get("State", {}) or {}
    if isinstance(state, dict):
        status = str(state.get("Status", "")).strip() or "unknown"
    else:
        status = "unknown"
    return container_id, status


def build_row(index: int, prefix: str) -> MappingRow:
    container_name = f"{prefix}{index}"
    container_id, container_status = inspect_container(container_name)
    return MappingRow(
        node_id=node_id_from_index(index),
        node_index=index,
        container_name=container_name,
        container_id=container_id,
        container_status=container_status,
    )


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    rows: list[MappingRow] = []
    with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as pool:
        futures = [pool.submit(build_row, idx, args.container_prefix) for idx in range(1, int(args.count) + 1)]
        for fut in futures:
            rows.append(fut.result())

    rows.sort(key=lambda row: row.node_index)
    with output.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["node_id", "node_index", "container_name", "container_id", "container_status"])
        for row in rows:
            writer.writerow(
                [
                    row.node_id,
                    row.node_index,
                    row.container_name,
                    row.container_id,
                    row.container_status,
                ]
            )

    missing = [row.container_name for row in rows if row.container_status == "missing"]
    print(f"wrote {len(rows)} rows to {output}")
    if missing:
        sample = ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else "")
        print(f"[warn] missing containers={len(missing)} sample={sample}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
