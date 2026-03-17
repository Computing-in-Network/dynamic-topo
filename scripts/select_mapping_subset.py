#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import deque
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select a deterministic subset from a node mapping CSV.")
    parser.add_argument("--input", default="docs/node_mapping_300.csv", help="Source mapping CSV")
    parser.add_argument("--output", required=True, help="Subset mapping CSV to write")
    parser.add_argument("--max-nodes", type=int, default=10, help="How many rows to keep after sorting by node_index")
    parser.add_argument(
        "--strategy",
        default="same-plane-ring",
        choices=("same-plane-ring", "first-n", "largest-component-snapshot"),
        help="Subset selection strategy",
    )
    parser.add_argument(
        "--snapshot",
        default="",
        help="Topology snapshot JSON used by largest-component-snapshot strategy",
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


def _load_snapshot_payload(snapshot_path: Path) -> dict:
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[fatal] invalid snapshot json: {snapshot_path}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"[fatal] invalid snapshot root: {snapshot_path}")
    return payload


def _largest_component_node_ids(rows: list[dict[str, str]], snapshot_path: Path) -> list[str]:
    payload = _load_snapshot_payload(snapshot_path)

    component_nodes = payload.get("component_nodes")
    if isinstance(component_nodes, list):
        node_ids = [str(item) for item in component_nodes if isinstance(item, str)]
        if node_ids:
            return node_ids

    known_nodes = {str(row["node_id"]) for row in rows}
    edges = payload.get("edges")
    if not isinstance(edges, list):
        raise SystemExit(f"[fatal] snapshot missing edges: {snapshot_path}")

    adj: dict[str, set[str]] = {node_id: set() for node_id in known_nodes}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        a = str(edge.get("a") or "")
        b = str(edge.get("b") or "")
        if a not in known_nodes or b not in known_nodes or a == b:
            continue
        adj[a].add(b)
        adj[b].add(a)

    best: list[str] = []
    seen: set[str] = set()
    ordered_nodes = [str(row["node_id"]) for row in rows]
    for node_id in ordered_nodes:
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


def _select_largest_component_snapshot(
    rows: list[dict[str, str]],
    *,
    max_nodes: int,
    snapshot_path: Path,
) -> list[dict[str, str]]:
    if not snapshot_path.is_file():
        raise SystemExit(f"[fatal] snapshot not found: {snapshot_path}")

    payload = _load_snapshot_payload(snapshot_path)
    component_node_list = _largest_component_node_ids(rows, snapshot_path)
    component_nodes = set(component_node_list)
    rows_by_id = {str(row["node_id"]): row for row in rows}
    if len(component_nodes) < max_nodes:
        raise SystemExit(
            f"[fatal] snapshot largest component too small: size={len(component_nodes)} required={max_nodes} path={snapshot_path}"
        )

    ordered_component = sorted(
        (node_id for node_id in component_node_list if node_id in rows_by_id),
        key=lambda node_id: int(rows_by_id[node_id]["node_index"]),
    )
    if not ordered_component:
        raise SystemExit(f"[fatal] snapshot component does not intersect mapping rows: {snapshot_path}")

    edges = payload.get("edges")
    if not isinstance(edges, list):
        raise SystemExit(f"[fatal] snapshot missing edges: {snapshot_path}")
    adj: dict[str, set[str]] = {node_id: set() for node_id in ordered_component}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        a = str(edge.get("a") or "")
        b = str(edge.get("b") or "")
        if a not in adj or b not in adj or a == b:
            continue
        adj[a].add(b)
        adj[b].add(a)

    start = ordered_component[0]
    queue: deque[str] = deque([start])
    visited: set[str] = {start}
    chosen: list[str] = []
    while queue and len(chosen) < max_nodes:
        cur = queue.popleft()
        chosen.append(cur)
        neighbors = sorted(adj[cur], key=lambda node_id: int(rows_by_id[node_id]["node_index"]))
        for nxt in neighbors:
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.append(nxt)

    if len(chosen) < max_nodes:
        raise SystemExit(
            f"[fatal] cannot extract connected subset from snapshot: size={len(chosen)} required={max_nodes} path={snapshot_path}"
        )

    kept = [rows_by_id[node_id] for node_id in chosen]
    kept.sort(key=lambda row: int(row["node_index"]))
    return kept


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
    elif args.strategy == "largest-component-snapshot":
        kept = _select_largest_component_snapshot(
            rows,
            max_nodes=max_nodes,
            snapshot_path=Path(args.snapshot),
        )
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
