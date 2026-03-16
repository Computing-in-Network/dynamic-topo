#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select a deterministic subset from a node mapping CSV.")
    parser.add_argument("--input", default="docs/node_mapping_300.csv", help="Source mapping CSV")
    parser.add_argument("--output", required=True, help="Subset mapping CSV to write")
    parser.add_argument("--max-nodes", type=int, default=10, help="How many rows to keep after sorting by node_index")
    parser.add_argument(
        "--strategy",
        default="same-plane-ring",
        choices=("same-plane-ring", "first-n"),
        help="Subset selection strategy",
    )
    return parser.parse_args()


_POLAR_RE = re.compile(r"^SAT-POLAR-(\d{3})$")


def _choose_plane_count(count: int) -> int:
    root = int(count**0.5)
    for p in range(root, 0, -1):
        if count % p == 0:
            return p
    return max(1, root)


def _select_same_plane_ring(rows: list[dict[str, str]], max_nodes: int) -> list[dict[str, str]]:
    polar_rows = [row for row in rows if _POLAR_RE.match(str(row.get("node_id") or ""))]
    if len(polar_rows) < max_nodes:
        return rows[:max_nodes]

    polar_rows.sort(key=lambda row: int(row["node_index"]))
    planes = _choose_plane_count(len(polar_rows))
    if planes <= 0:
        return rows[:max_nodes]

    selected: list[dict[str, str]] = []
    for slot in range(0, len(polar_rows), planes):
        selected.append(polar_rows[slot])
        if len(selected) >= max_nodes:
            break
    if len(selected) < max_nodes:
        return rows[:max_nodes]
    return selected


def main() -> int:
    args = parse_args()
    src = Path(args.input)
    dst = Path(args.output)

    with src.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    if not rows:
        raise SystemExit(f"[fatal] empty mapping csv: {src}")
    if "node_index" not in fieldnames:
        raise SystemExit(f"[fatal] missing node_index column: {src}")

    rows.sort(key=lambda row: int(row["node_index"]))
    max_nodes = max(0, int(args.max_nodes))
    if args.strategy == "same-plane-ring":
        kept = _select_same_plane_ring(rows, max_nodes=max_nodes)
    else:
        kept = rows[:max_nodes]
    if not kept:
        raise SystemExit(f"[fatal] no rows selected from {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)

    print(f"input={src} output={dst} rows={len(kept)} strategy={args.strategy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
