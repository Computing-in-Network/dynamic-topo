#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select a deterministic subset from a node mapping CSV.")
    parser.add_argument("--input", default="docs/node_mapping_300.csv", help="Source mapping CSV")
    parser.add_argument("--output", required=True, help="Subset mapping CSV to write")
    parser.add_argument("--max-nodes", type=int, default=10, help="How many rows to keep after sorting by node_index")
    return parser.parse_args()


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
    kept = rows[: max(0, int(args.max_nodes))]
    if not kept:
        raise SystemExit(f"[fatal] no rows selected from {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)

    print(f"input={src} output={dst} rows={len(kept)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
