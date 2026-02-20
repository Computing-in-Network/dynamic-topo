from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Dict, List

import numpy as np
from pyproj import Transformer

from .storage import create_redis_client

EARTH_RADIUS_M = 6_371_000.0
EARTH_ROT_RATE = 7.2921150e-5
NODE_TYPE_LEO = 0
NODE_TYPE_AIR = 1
NODE_TYPE_SHIP = 2


@dataclass(frozen=True)
class SimulationConfig:
    total_nodes: int = 300
    leo_polar_count: int = 100
    leo_inclined_count: int = 100
    aircraft_count: int = 50
    ship_count: int = 50
    leo_altitude_m: float = 550_000.0
    aircraft_altitude_m: float = 10_000.0
    ship_altitude_m: float = 0.0
    aircraft_speed_mps: float = 250.0
    ship_speed_mps: float = 10.0
    timestep_s: float = 1.0
    redis_url: str = "redis://localhost:6379/0"

    # Base link feasibility constraints.
    dmax_leo_leo_m: float = 5_000_000.0
    dmax_leo_air_m: float = 3_000_000.0
    dmax_leo_ship_m: float = 2_800_000.0
    dmax_air_air_m: float = 700_000.0
    dmax_air_ship_m: float = 400_000.0
    dmax_ship_ship_m: float = 80_000.0

    # Capacity constraints.
    max_neighbors_leo: int = 8
    max_neighbors_air: int = 4
    max_neighbors_ship: int = 3

    # Satellite beam model (nadir-pointing cone towards Earth center).
    sat_beam_half_angle_deg: float = 45.0
    sat_beam_slots: int = 16

    # Link state hysteresis.
    up_hold_s: float = 2.0
    down_hold_s: float = 2.0


@dataclass(frozen=True)
class TickResult:
    sim_time_s: float
    node_positions_ecef: np.ndarray
    adjacency: np.ndarray
    elapsed_ms: float


@dataclass(frozen=True)
class TopologyFrame:
    sim_time_s: float
    elapsed_ms: float
    nodes: List[dict]
    links: List[dict]
    metrics: dict


class TopologyEngine:
    def __init__(self, config: SimulationConfig, seed: int = 42, redis_client=None):
        self.config = config
        self._rng = np.random.default_rng(seed)
        self._redis = redis_client if redis_client is not None else create_redis_client(config.redis_url)
        self._lla_to_ecef = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
        self._ecef_to_lla = Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)

        self._init_satellite_state()
        self._init_mobile_state()
        self._init_node_meta()
        self._init_link_state()

    def _init_satellite_state(self) -> None:
        cfg = self.config
        self._sat_count = cfg.leo_polar_count + cfg.leo_inclined_count
        inclinations_deg = np.concatenate(
            [
                np.full(cfg.leo_polar_count, 97.6, dtype=np.float64),
                np.full(cfg.leo_inclined_count, 53.0, dtype=np.float64),
            ]
        )
        self._sat_inclinations = np.deg2rad(inclinations_deg)

        self._sat_radius_m = np.full(self._sat_count, EARTH_RADIUS_M + cfg.leo_altitude_m, dtype=np.float64)
        self._sat_raan = self._rng.uniform(0.0, 2.0 * np.pi, size=self._sat_count)
        self._sat_phase = self._rng.uniform(0.0, 2.0 * np.pi, size=self._sat_count)

        mu = 3.986004418e14
        self._sat_angular_rate = np.sqrt(mu / np.power(self._sat_radius_m, 3))

    def _init_mobile_state(self) -> None:
        cfg = self.config
        mobile_count = cfg.aircraft_count + cfg.ship_count
        self._mobile_lat_rad = np.deg2rad(self._rng.uniform(-70.0, 70.0, size=mobile_count))
        self._mobile_lon_rad = np.deg2rad(self._rng.uniform(-180.0, 180.0, size=mobile_count))
        self._mobile_heading = self._rng.uniform(0.0, 2.0 * np.pi, size=mobile_count)
        self._mobile_speed = np.concatenate(
            [
                np.full(cfg.aircraft_count, cfg.aircraft_speed_mps, dtype=np.float64),
                np.full(cfg.ship_count, cfg.ship_speed_mps, dtype=np.float64),
            ]
        )
        self._mobile_altitude = np.concatenate(
            [
                np.full(cfg.aircraft_count, cfg.aircraft_altitude_m, dtype=np.float64),
                np.full(cfg.ship_count, cfg.ship_altitude_m, dtype=np.float64),
            ]
        )

    def _init_node_meta(self) -> None:
        cfg = self.config
        node_count = cfg.leo_polar_count + cfg.leo_inclined_count + cfg.aircraft_count + cfg.ship_count
        if cfg.total_nodes != node_count:
            raise ValueError(f"total_nodes={cfg.total_nodes} but counts sum to {node_count}")

        self._type_codes = np.concatenate(
            [
                np.full(self._sat_count, NODE_TYPE_LEO, dtype=np.int8),
                np.full(cfg.aircraft_count, NODE_TYPE_AIR, dtype=np.int8),
                np.full(cfg.ship_count, NODE_TYPE_SHIP, dtype=np.int8),
            ]
        )
        self._is_sat = self._type_codes == NODE_TYPE_LEO

        self._degree_caps = np.where(
            self._type_codes == NODE_TYPE_LEO,
            cfg.max_neighbors_leo,
            np.where(self._type_codes == NODE_TYPE_AIR, cfg.max_neighbors_air, cfg.max_neighbors_ship),
        )

        dmax = np.array(
            [
                [cfg.dmax_leo_leo_m, cfg.dmax_leo_air_m, cfg.dmax_leo_ship_m],
                [cfg.dmax_leo_air_m, cfg.dmax_air_air_m, cfg.dmax_air_ship_m],
                [cfg.dmax_leo_ship_m, cfg.dmax_air_ship_m, cfg.dmax_ship_ship_m],
            ],
            dtype=np.float64,
        )
        self._dmax_matrix = dmax[self._type_codes[:, None], self._type_codes[None, :]]
        self._beam_cos_threshold = float(np.cos(np.deg2rad(cfg.sat_beam_half_angle_deg)))

    def _init_link_state(self) -> None:
        n = self.config.total_nodes
        self._adj_prev = np.zeros((n, n), dtype=bool)
        self._up_count = np.zeros((n, n), dtype=np.uint8)
        self._down_count = np.zeros((n, n), dtype=np.uint8)
        self._up_hold_ticks = max(1, int(np.ceil(self.config.up_hold_s / self.config.timestep_s)))
        self._down_hold_ticks = max(1, int(np.ceil(self.config.down_hold_s / self.config.timestep_s)))

    @property
    def node_type_names(self) -> List[str]:
        return [
            "leo" if code == NODE_TYPE_LEO else ("aircraft" if code == NODE_TYPE_AIR else "ship")
            for code in self._type_codes.tolist()
        ]

    @property
    def node_ids(self) -> List[str]:
        cfg = self.config
        ids: List[str] = []
        ids.extend([f"L1-{i:03d}" for i in range(cfg.leo_polar_count)])
        ids.extend([f"L2-{i:03d}" for i in range(cfg.leo_inclined_count)])
        ids.extend([f"A1-{i:03d}" for i in range(cfg.aircraft_count)])
        ids.extend([f"S1-{i:03d}" for i in range(cfg.ship_count)])
        return ids

    def step(self, sim_time_s: float) -> TickResult:
        start = perf_counter()

        sat_positions = self._satellite_ecef(sim_time_s)
        mobile_positions = self._mobile_ecef(sim_time_s)
        node_positions = np.vstack([sat_positions, mobile_positions])

        adjacency = self._adjacency_from_positions(node_positions)
        self._write_state_to_redis(sim_time_s, node_positions, adjacency)

        elapsed_ms = (perf_counter() - start) * 1000.0
        return TickResult(sim_time_s=sim_time_s, node_positions_ecef=node_positions, adjacency=adjacency, elapsed_ms=elapsed_ms)

    def run_steps(self, steps: int, start_time_s: float = 0.0) -> List[TickResult]:
        results: List[TickResult] = []
        sim_time = start_time_s
        for _ in range(steps):
            results.append(self.step(sim_time))
            sim_time += self.config.timestep_s
        return results

    def build_frame(self, result: TickResult) -> TopologyFrame:
        positions = result.node_positions_ecef
        x = positions[:, 0]
        y = positions[:, 1]
        z = positions[:, 2]
        lon, lat, alt = self._ecef_to_lla.transform(x, y, z)

        node_types = self.node_type_names
        node_ids = self.node_ids
        nodes: List[dict] = []
        for idx, node_id in enumerate(node_ids):
            nodes.append(
                {
                    "id": node_id,
                    "type": node_types[idx],
                    "x": float(x[idx]),
                    "y": float(y[idx]),
                    "z": float(z[idx]),
                    "lat": float(lat[idx]),
                    "lon": float(lon[idx]),
                    "alt_m": float(alt[idx]),
                }
            )

        ai, aj = np.where(np.triu(result.adjacency, k=1))
        links = [{"a": node_ids[int(i)], "b": node_ids[int(j)]} for i, j in zip(ai, aj, strict=True)]
        degree = result.adjacency.sum(axis=1)
        metrics = {
            "edge_count": int(len(links)),
            "avg_degree": float(np.mean(degree)),
            "max_degree": int(np.max(degree)),
        }

        return TopologyFrame(
            sim_time_s=result.sim_time_s,
            elapsed_ms=result.elapsed_ms,
            nodes=nodes,
            links=links,
            metrics=metrics,
        )

    def _satellite_ecef(self, sim_time_s: float) -> np.ndarray:
        theta = self._sat_phase + self._sat_angular_rate * sim_time_s
        cos_t = np.cos(theta)
        sin_t = np.sin(theta)

        x_orb = self._sat_radius_m * cos_t
        y_orb = self._sat_radius_m * sin_t

        cos_i = np.cos(self._sat_inclinations)
        sin_i = np.sin(self._sat_inclinations)
        cos_raan = np.cos(self._sat_raan)
        sin_raan = np.sin(self._sat_raan)

        x_eci = cos_raan * x_orb - sin_raan * cos_i * y_orb
        y_eci = sin_raan * x_orb + cos_raan * cos_i * y_orb
        z_eci = sin_i * y_orb

        gmst = EARTH_ROT_RATE * sim_time_s
        c = np.cos(gmst)
        s = np.sin(gmst)
        x_ecef = c * x_eci + s * y_eci
        y_ecef = -s * x_eci + c * y_eci

        return np.column_stack([x_ecef, y_ecef, z_eci])

    def _mobile_ecef(self, sim_time_s: float) -> np.ndarray:
        # Great-circle approximation for constant-speed random tracks.
        dist = self._mobile_speed * sim_time_s
        ang = dist / EARTH_RADIUS_M

        lat1 = self._mobile_lat_rad
        lon1 = self._mobile_lon_rad
        brng = self._mobile_heading

        sin_lat1 = np.sin(lat1)
        cos_lat1 = np.cos(lat1)
        sin_ang = np.sin(ang)
        cos_ang = np.cos(ang)

        lat2 = np.arcsin(sin_lat1 * cos_ang + cos_lat1 * sin_ang * np.cos(brng))
        lon2 = lon1 + np.arctan2(
            np.sin(brng) * sin_ang * cos_lat1,
            cos_ang - sin_lat1 * np.sin(lat2),
        )
        lon2 = (lon2 + np.pi) % (2.0 * np.pi) - np.pi

        lon_deg = np.rad2deg(lon2)
        lat_deg = np.rad2deg(lat2)
        x, y, z = self._lla_to_ecef.transform(lon_deg, lat_deg, self._mobile_altitude)
        return np.column_stack([x, y, z])

    def _adjacency_from_positions(self, positions: np.ndarray) -> np.ndarray:
        los, dist, delta = self._geometry_matrices(positions)
        candidate = los & (dist <= self._dmax_matrix)
        np.fill_diagonal(candidate, False)

        beam_ok = self._satellite_beam_mask(positions, delta)
        candidate &= beam_ok

        capped = self._apply_capacity_constraints(candidate, dist)
        stable = self._stabilize_links(capped)
        return stable

    def _geometry_matrices(self, positions: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        p1 = positions[:, np.newaxis, :]
        p2 = positions[np.newaxis, :, :]
        delta = p2 - p1

        seg_len_sq = np.sum(delta * delta, axis=-1)
        seg_len_sq = np.where(seg_len_sq == 0.0, 1.0, seg_len_sq)
        dist = np.sqrt(seg_len_sq)

        t = -np.sum(p1 * delta, axis=-1) / seg_len_sq
        t = np.clip(t, 0.0, 1.0)
        closest = p1 + t[..., np.newaxis] * delta

        closest_sq = np.sum(closest * closest, axis=-1)
        los = closest_sq > (EARTH_RADIUS_M * EARTH_RADIUS_M)
        np.fill_diagonal(los, False)

        return los, dist, delta

    def _satellite_beam_mask(self, positions: np.ndarray, delta: np.ndarray) -> np.ndarray:
        n = positions.shape[0]
        mask = np.ones((n, n), dtype=bool)
        sat_idx = np.where(self._is_sat)[0]
        non_sat_idx = np.where(~self._is_sat)[0]
        if sat_idx.size == 0 or non_sat_idx.size == 0:
            return mask

        sat_pos = positions[sat_idx]
        sat_to_target = delta[np.ix_(sat_idx, non_sat_idx)]

        nadir = -sat_pos
        nadir_norm = np.linalg.norm(nadir, axis=1, keepdims=True)
        st_norm = np.linalg.norm(sat_to_target, axis=2)
        safe_norm = np.where(st_norm == 0.0, 1.0, st_norm)

        dot = np.sum(sat_to_target * nadir[:, None, :], axis=2)
        cos_angle = dot / (safe_norm * nadir_norm)
        in_beam = cos_angle >= self._beam_cos_threshold

        mask[np.ix_(sat_idx, non_sat_idx)] = in_beam
        mask[np.ix_(non_sat_idx, sat_idx)] = in_beam.T
        np.fill_diagonal(mask, False)
        return mask

    def _apply_capacity_constraints(self, candidate: np.ndarray, dist: np.ndarray) -> np.ndarray:
        n = self.config.total_nodes
        selected = np.zeros((n, n), dtype=bool)
        degree = np.zeros(n, dtype=np.int16)
        sat_mobile_edges = np.zeros(self._sat_count, dtype=np.int16)

        iu, ju = np.triu_indices(n, k=1)
        valid = candidate[iu, ju]
        if not np.any(valid):
            return selected

        ei = iu[valid]
        ej = ju[valid]

        prev_bonus = self._adj_prev[ei, ej].astype(np.float64) * 10_000.0
        score = prev_bonus - dist[ei, ej]
        order = np.argsort(score)[::-1]

        for k in order:
            i = int(ei[k])
            j = int(ej[k])
            if degree[i] >= self._degree_caps[i] or degree[j] >= self._degree_caps[j]:
                continue

            i_is_sat = i < self._sat_count
            j_is_sat = j < self._sat_count

            if i_is_sat and not j_is_sat and sat_mobile_edges[i] >= self.config.sat_beam_slots:
                continue
            if j_is_sat and not i_is_sat and sat_mobile_edges[j] >= self.config.sat_beam_slots:
                continue

            selected[i, j] = True
            selected[j, i] = True
            degree[i] += 1
            degree[j] += 1

            if i_is_sat and not j_is_sat:
                sat_mobile_edges[i] += 1
            if j_is_sat and not i_is_sat:
                sat_mobile_edges[j] += 1

        np.fill_diagonal(selected, False)
        return selected

    def _stabilize_links(self, candidate: np.ndarray) -> np.ndarray:
        prev = self._adj_prev

        self._up_count = np.where(~prev & candidate, np.minimum(self._up_count + 1, 255), 0).astype(np.uint8)
        self._down_count = np.where(prev & ~candidate, np.minimum(self._down_count + 1, 255), 0).astype(np.uint8)

        new_adj = prev.copy()
        promote = (~prev) & (self._up_count >= self._up_hold_ticks)
        demote = prev & (self._down_count >= self._down_hold_ticks)

        new_adj[promote] = True
        new_adj[demote] = False

        np.fill_diagonal(new_adj, False)
        new_adj = np.logical_or(new_adj, new_adj.T)
        self._adj_prev = new_adj
        return new_adj

    def _write_state_to_redis(self, sim_time_s: float, positions: np.ndarray, adjacency: np.ndarray) -> None:
        mapping: Dict[str, str] = {}
        for node_id, pos in zip(self.node_ids, positions, strict=True):
            mapping[node_id] = f"{pos[0]:.3f},{pos[1]:.3f},{pos[2]:.3f}"
        self._redis.hset("node:pos", mapping=mapping)

        bitmap = np.packbits(adjacency.astype(np.uint8).reshape(-1), bitorder="little")
        self._redis.xadd(
            "topo:adjacency",
            {
                "ts": f"{sim_time_s:.1f}",
                "n": str(self.config.total_nodes),
                "bitmap_hex": bitmap.tobytes().hex(),
            },
        )


def estimated_working_set_mb(node_count: int = 300) -> float:
    # Main NxN tensors in adjacency computation: delta(3), t(1), closest(3), los(1), dist(1).
    bytes_per_pair = (3 + 1 + 3 + 1) * 8 + 1
    total_bytes = node_count * node_count * bytes_per_pair
    return total_bytes / (1024 * 1024)
