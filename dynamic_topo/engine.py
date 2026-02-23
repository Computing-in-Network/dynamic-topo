from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from time import perf_counter
from typing import Dict, List

import numpy as np
from pyproj import Transformer
from sgp4.api import WGS72, Satrec
from skyfield.api import EarthSatellite, load
from skyfield.framelib import itrs

from .link_policy import LinkPolicy, load_link_policy_file
from .storage import create_redis_client

EARTH_RADIUS_M = 6_371_000.0
NODE_TYPE_LEO = 0
NODE_TYPE_AIR = 1
NODE_TYPE_SHIP = 2

try:  # pragma: no cover - optional dependency
    from global_land_mask import globe as _land_globe  # type: ignore
except Exception:  # pragma: no cover
    _land_globe = None


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
    dmax_leo_air_m: float = 2_800_000.0
    dmax_leo_ship_m: float = 2_700_000.0
    dmax_air_air_m: float = 700_000.0
    dmax_air_ship_m: float = 400_000.0
    dmax_ship_ship_m: float = 80_000.0

    # Capacity constraints.
    max_neighbors_leo: int = 10
    sat_isl_ports: int = 4
    max_neighbors_air: int = 4
    max_neighbors_ship: int = 3

    # Satellite beam model (nadir-pointing cone towards Earth center).
    sat_beam_half_angle_deg: float = 66.5
    sat_beam_slots: int = 24

    # Link state hysteresis.
    up_hold_s: float = 2.0
    down_hold_s: float = 2.0
    min_link_up_s: float = 0.0
    min_link_down_s: float = 0.0

    # Optional movement constraints.
    enforce_ship_ocean_mask: bool = True
    link_policy_path: str | None = None
    link_policy_hot_reload: bool = False
    incremental_geometry: bool = False
    incremental_move_threshold_m: float = 1e-6
    incremental_rebuild_ratio: float = 0.35


@dataclass(frozen=True)
class TickResult:
    sim_time_s: float
    node_positions_ecef: np.ndarray
    adjacency: np.ndarray
    elapsed_ms: float
    satellite_velocity_ecef: np.ndarray | None = None


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
        self._timescale = load.timescale()
        self._epoch_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._redis = redis_client if redis_client is not None else create_redis_client(config.redis_url)
        self._lla_to_ecef = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
        self._ecef_to_lla = Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)
        self._default_link_policy = LinkPolicy.from_simulation_config(config)
        self._link_policy = self._default_link_policy
        self._link_policy_path = config.link_policy_path
        self._link_policy_mtime_ns: int | None = None
        self._load_link_policy_if_configured()

        self._init_satellite_state()
        self._init_mobile_state()
        self._init_node_meta()
        self._init_link_state()

    def _load_link_policy_if_configured(self) -> None:
        if not self._link_policy_path:
            return
        self._link_policy = load_link_policy_file(self._link_policy_path, base=self._default_link_policy)
        self._link_policy_mtime_ns = os.stat(self._link_policy_path).st_mtime_ns

    def _reload_link_policy_if_needed(self) -> None:
        if not self.config.link_policy_hot_reload or not self._link_policy_path:
            return
        current_mtime_ns = os.stat(self._link_policy_path).st_mtime_ns
        if self._link_policy_mtime_ns is not None and current_mtime_ns == self._link_policy_mtime_ns:
            return

        new_policy = load_link_policy_file(self._link_policy_path, base=self._default_link_policy)
        self._link_policy_mtime_ns = current_mtime_ns
        if new_policy == self._link_policy:
            return

        self._link_policy = new_policy
        self._apply_link_policy(reset_hysteresis=True)
        print(f"reloaded link policy from {self._link_policy_path}")

    def _init_satellite_state(self) -> None:
        cfg = self.config
        self._sat_count = cfg.leo_polar_count + cfg.leo_inclined_count
        self._polar_count = cfg.leo_polar_count
        self._incl_count = cfg.leo_inclined_count
        self._sat_orbit_class = (
            ["polar"] * cfg.leo_polar_count + ["inclined"] * cfg.leo_inclined_count
        )
        self._sat_orbit_code = np.concatenate(
            [
                np.zeros(cfg.leo_polar_count, dtype=np.int8),
                np.ones(cfg.leo_inclined_count, dtype=np.int8),
            ]
        )
        self._polar_planes = self._choose_plane_count(cfg.leo_polar_count) if cfg.leo_polar_count > 0 else 0
        self._incl_planes = self._choose_plane_count(cfg.leo_inclined_count) if cfg.leo_inclined_count > 0 else 0
        self._polar_sats_per_plane = (
            cfg.leo_polar_count // self._polar_planes if self._polar_planes > 0 else 0
        )
        self._incl_sats_per_plane = (
            cfg.leo_inclined_count // self._incl_planes if self._incl_planes > 0 else 0
        )
        self._polar_sat_idx = np.arange(cfg.leo_polar_count, dtype=np.int32)
        self._all_sat_idx = np.arange(self._sat_count, dtype=np.int32)
        self._sat_inclinations_deg = np.concatenate(
            [
                np.full(cfg.leo_polar_count, 97.6, dtype=np.float64),
                np.full(cfg.leo_inclined_count, 53.0, dtype=np.float64),
            ]
        )
        mean_motion_rad_min = self._mean_motion_rad_min(cfg.leo_altitude_m)
        self._satellites: List[EarthSatellite] = []
        self._satellites.extend(
            self._build_satellite_group(
                count=cfg.leo_polar_count,
                inclination_deg=97.6,
                satnum_start=10_000,
                mean_motion_rad_min=mean_motion_rad_min,
            )
        )
        self._satellites.extend(
            self._build_satellite_group(
                count=cfg.leo_inclined_count,
                inclination_deg=53.0,
                satnum_start=20_000,
                mean_motion_rad_min=mean_motion_rad_min,
            )
        )

    def _mean_motion_rad_min(self, altitude_m: float) -> float:
        mu = 3.986004418e14
        semi_major_axis_m = EARTH_RADIUS_M + altitude_m
        n_rad_s = np.sqrt(mu / np.power(semi_major_axis_m, 3))
        return float(n_rad_s * 60.0)

    def _build_satellite_group(
        self,
        count: int,
        inclination_deg: float,
        satnum_start: int,
        mean_motion_rad_min: float,
    ) -> List[EarthSatellite]:
        if count <= 0:
            return []

        planes = self._choose_plane_count(count)
        sats_per_plane = int(np.ceil(count / planes))
        epoch_days = self._days_since_sgp4_epoch(self._epoch_dt)
        satellites: List[EarthSatellite] = []
        for idx in range(count):
            plane = idx % planes
            slot = idx // planes
            raan_rad = np.deg2rad((360.0 / planes) * plane)
            mean_anomaly_rad = np.deg2rad((360.0 / sats_per_plane) * slot)

            satrec = Satrec()
            satrec.sgp4init(
                WGS72,
                "i",
                satnum_start + idx,
                epoch_days,
                0.0,
                0.0,
                0.0,
                0.0001,
                0.0,
                np.deg2rad(inclination_deg),
                mean_anomaly_rad,
                mean_motion_rad_min,
                raan_rad,
            )
            satellites.append(EarthSatellite.from_satrec(satrec, self._timescale))
        return satellites

    def _choose_plane_count(self, count: int) -> int:
        # Choose a near-square plane count for clear orbital grouping.
        root = int(np.sqrt(count))
        for p in range(root, 0, -1):
            if count % p == 0:
                return p
        return max(1, root)

    def _days_since_sgp4_epoch(self, dt: datetime) -> float:
        sgp4_epoch = datetime(1949, 12, 31, tzinfo=timezone.utc)
        return (dt - sgp4_epoch).total_seconds() / 86400.0

    def _is_land(self, lat_deg: float, lon_deg: float) -> bool:
        if _land_globe is not None:
            return bool(_land_globe.is_land(lat_deg, lon_deg))
        return self._is_land_fallback(lat_deg, lon_deg)

    def _is_land_fallback(self, lat_deg: float, lon_deg: float) -> bool:
        # Coarse continent envelopes used only when no dedicated land-mask library is installed.
        envelopes = (
            (-35.0, 37.0, -18.0, 52.0),   # Africa
            (-10.0, 40.0, 35.0, 75.0),    # Europe
            (5.0, 75.0, 25.0, 180.0),     # Asia
            (15.0, 72.0, -170.0, -50.0),  # North America
            (-56.0, 13.0, -82.0, -34.0),  # South America
            (-45.0, -10.0, 113.0, 154.0), # Australia
            (-85.0, -60.0, -180.0, 180.0),# Antarctica fringe
            (59.0, 84.0, -74.0, -10.0),   # Greenland
        )
        for lat_min, lat_max, lon_min, lon_max in envelopes:
            if lat_min <= lat_deg <= lat_max and lon_min <= lon_deg <= lon_max:
                return True
        return False

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
        self._ship_slice = slice(cfg.aircraft_count, mobile_count)
        self._mobile_lat0_rad = self._mobile_lat_rad.copy()
        self._mobile_lon0_rad = self._mobile_lon_rad.copy()
        self._mobile_last_time_s: float | None = None

        if cfg.enforce_ship_ocean_mask and cfg.ship_count > 0:
            self._initialize_ships_on_ocean()

    def _initialize_ships_on_ocean(self) -> None:
        for i in range(self.config.aircraft_count, self.config.aircraft_count + self.config.ship_count):
            for _ in range(2_000):
                lat_deg = float(self._rng.uniform(-70.0, 70.0))
                lon_deg = float(self._rng.uniform(-180.0, 180.0))
                if not self._is_land(lat_deg, lon_deg):
                    self._mobile_lat_rad[i] = np.deg2rad(lat_deg)
                    self._mobile_lon_rad[i] = np.deg2rad(lon_deg)
                    break
        self._mobile_lat0_rad = self._mobile_lat_rad.copy()
        self._mobile_lon0_rad = self._mobile_lon_rad.copy()

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

    def _init_link_state(self) -> None:
        n = self.config.total_nodes
        self._adj_prev = np.zeros((n, n), dtype=bool)
        self._up_count = np.zeros((n, n), dtype=np.uint8)
        self._down_count = np.zeros((n, n), dtype=np.uint8)
        self._state_age_ticks = np.full((n, n), np.uint16(65535), dtype=np.uint16)
        self._last_flip_count = 0
        self._apply_link_policy(reset_hysteresis=True)
        self._init_incremental_cache()

    def _init_incremental_cache(self) -> None:
        self._cached_positions: np.ndarray | None = None
        self._cached_los: np.ndarray | None = None
        self._cached_dist: np.ndarray | None = None
        self._cached_delta: np.ndarray | None = None

    def _apply_link_policy(self, reset_hysteresis: bool) -> None:
        p = self._link_policy
        self._degree_caps = np.where(
            self._type_codes == NODE_TYPE_LEO,
            p.max_neighbors_leo,
            np.where(self._type_codes == NODE_TYPE_AIR, p.max_neighbors_air, p.max_neighbors_ship),
        )

        dmax = np.array(
            [
                [p.dmax_leo_leo_m, p.dmax_leo_air_m, p.dmax_leo_ship_m],
                [p.dmax_leo_air_m, p.dmax_air_air_m, p.dmax_air_ship_m],
                [p.dmax_leo_ship_m, p.dmax_air_ship_m, p.dmax_ship_ship_m],
            ],
            dtype=np.float64,
        )
        self._dmax_matrix = dmax[self._type_codes[:, None], self._type_codes[None, :]]
        self._beam_cos_threshold = float(np.cos(np.deg2rad(p.sat_beam_half_angle_deg)))
        self._up_hold_ticks = max(1, int(np.ceil(p.up_hold_s / self.config.timestep_s)))
        self._down_hold_ticks = max(1, int(np.ceil(p.down_hold_s / self.config.timestep_s)))
        self._min_link_up_ticks = max(0, int(np.ceil(p.min_link_up_s / self.config.timestep_s)))
        self._min_link_down_ticks = max(0, int(np.ceil(p.min_link_down_s / self.config.timestep_s)))

        if reset_hysteresis:
            self._up_count.fill(0)
            self._down_count.fill(0)
            self._state_age_ticks.fill(np.uint16(65535))
            self._last_flip_count = 0

    @property
    def node_type_names(self) -> List[str]:
        return [
            "leo" if code == NODE_TYPE_LEO else ("aircraft" if code == NODE_TYPE_AIR else "ship")
            for code in self._type_codes.tolist()
        ]

    @property
    def node_display_names(self) -> List[str]:
        cfg = self.config
        names: List[str] = []
        names.extend([f"SAT-POLAR-{i + 1:03d}" for i in range(cfg.leo_polar_count)])
        names.extend([f"SAT-INCL-{i + 1:03d}" for i in range(cfg.leo_inclined_count)])
        names.extend([f"AIR-{i + 1:03d}" for i in range(cfg.aircraft_count)])
        names.extend([f"SHIP-{i + 1:03d}" for i in range(cfg.ship_count)])
        return names

    @property
    def node_ids(self) -> List[str]:
        return self.node_display_names

    def step(self, sim_time_s: float, persist: bool = True) -> TickResult:
        start = perf_counter()
        self._reload_link_policy_if_needed()

        sat_positions, sat_velocity = self._satellite_ecef_with_velocity(sim_time_s)
        mobile_positions = self._mobile_ecef(sim_time_s)
        node_positions = np.vstack([sat_positions, mobile_positions])

        adjacency = self._adjacency_from_positions(node_positions)
        if persist:
            self.persist_state(sim_time_s, node_positions, adjacency)

        elapsed_ms = (perf_counter() - start) * 1000.0
        return TickResult(
            sim_time_s=sim_time_s,
            node_positions_ecef=node_positions,
            adjacency=adjacency,
            elapsed_ms=elapsed_ms,
            satellite_velocity_ecef=sat_velocity,
        )

    def run_steps(self, steps: int, start_time_s: float = 0.0, persist: bool = True) -> List[TickResult]:
        results: List[TickResult] = []
        sim_time = start_time_s
        for _ in range(steps):
            results.append(self.step(sim_time, persist=persist))
            sim_time += self.config.timestep_s
        return results

    def persist_state(self, sim_time_s: float, positions: np.ndarray, adjacency: np.ndarray) -> None:
        self._write_state_to_redis(sim_time_s, positions, adjacency)

    def build_frame(self, result: TickResult) -> TopologyFrame:
        positions = result.node_positions_ecef
        x = positions[:, 0]
        y = positions[:, 1]
        z = positions[:, 2]
        lon, lat, alt = self._ecef_to_lla.transform(x, y, z)

        node_types = self.node_type_names
        node_ids = self.node_ids
        node_labels = self.node_display_names
        nodes: List[dict] = []
        for idx, node_id in enumerate(node_ids):
            orbit_class = self._sat_orbit_class[idx] if idx < self._sat_count else None
            category = "satellite" if idx < self._sat_count else ("aircraft" if node_types[idx] == "aircraft" else "ship")
            vx = vy = vz = None
            if idx < self._sat_count and result.satellite_velocity_ecef is not None:
                vx = float(result.satellite_velocity_ecef[idx, 0])
                vy = float(result.satellite_velocity_ecef[idx, 1])
                vz = float(result.satellite_velocity_ecef[idx, 2])
            nodes.append(
                {
                    "id": node_id,
                    "name": node_labels[idx],
                    "type": node_types[idx],
                    "category": category,
                    "orbit_class": orbit_class,
                    "x": float(x[idx]),
                    "y": float(y[idx]),
                    "z": float(z[idx]),
                    "lat": float(lat[idx]),
                    "lon": float(lon[idx]),
                    "alt_m": float(alt[idx]),
                    "vx": vx,
                    "vy": vy,
                    "vz": vz,
                }
            )

        ai, aj = np.where(np.triu(result.adjacency, k=1))
        links = [{"a": node_ids[int(i)], "b": node_ids[int(j)]} for i, j in zip(ai, aj, strict=True)]
        degree = result.adjacency.sum(axis=1)
        metrics = {
            "edge_count": int(len(links)),
            "avg_degree": float(np.mean(degree)),
            "max_degree": int(np.max(degree)),
            "link_flip_count_tick": int(self._last_flip_count),
        }
        comp_count, largest_comp, largest_nodes = self._connected_components_summary(result.adjacency)
        metrics["component_count"] = int(comp_count)
        metrics["largest_component_size"] = int(largest_comp)
        metrics["largest_component_ratio"] = float(largest_comp / max(1, self.config.total_nodes))
        metrics["diameter_approx"] = int(self._approx_component_diameter(result.adjacency, largest_nodes))
        if self.config.aircraft_count + self.config.ship_count > 0:
            mobile_degree = degree[self._sat_count :]
            mobile_connected = int(np.sum(mobile_degree > 0))
            metrics["mobile_connected_count"] = mobile_connected
            metrics["mobile_connected_ratio"] = float(mobile_connected / mobile_degree.size)

        return TopologyFrame(
            sim_time_s=result.sim_time_s,
            elapsed_ms=result.elapsed_ms,
            nodes=nodes,
            links=links,
            metrics=metrics,
        )

    def _satellite_ecef_with_velocity(self, sim_time_s: float) -> tuple[np.ndarray, np.ndarray]:
        current_dt = self._epoch_dt + timedelta(seconds=float(sim_time_s))
        t = self._timescale.from_datetime(current_dt)
        positions = np.empty((self._sat_count, 3), dtype=np.float64)
        velocity = np.empty((self._sat_count, 3), dtype=np.float64)
        for idx, sat in enumerate(self._satellites):
            xyz, vel = sat.at(t).frame_xyz_and_velocity(itrs)
            positions[idx, 0] = float(xyz.m[0])
            positions[idx, 1] = float(xyz.m[1])
            positions[idx, 2] = float(xyz.m[2])
            velocity[idx, 0] = float(vel.m_per_s[0])
            velocity[idx, 1] = float(vel.m_per_s[1])
            velocity[idx, 2] = float(vel.m_per_s[2])
        return positions, velocity

    def _mobile_ecef(self, sim_time_s: float) -> np.ndarray:
        if self._mobile_last_time_s is None:
            self._mobile_last_time_s = float(sim_time_s)
        else:
            dt = float(sim_time_s - self._mobile_last_time_s)
            if dt < 0.0:
                self._mobile_lat_rad = self._mobile_lat0_rad.copy()
                self._mobile_lon_rad = self._mobile_lon0_rad.copy()
                self._mobile_last_time_s = 0.0
                dt = float(sim_time_s)
            if dt > 0.0:
                self._advance_mobile_state(dt)
                self._mobile_last_time_s = float(sim_time_s)

        lon_deg = np.rad2deg(self._mobile_lon_rad)
        lat_deg = np.rad2deg(self._mobile_lat_rad)
        x, y, z = self._lla_to_ecef.transform(lon_deg, lat_deg, self._mobile_altitude)
        return np.column_stack([x, y, z])

    def _advance_mobile_state(self, dt_s: float) -> None:
        dist = self._mobile_speed * dt_s
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

        if self.config.enforce_ship_ocean_mask and self.config.ship_count > 0:
            lat2, lon2 = self._adjust_ships_to_ocean(lat1, lon1, lat2, lon2, ang)

        self._mobile_lat_rad = lat2
        self._mobile_lon_rad = lon2

    def _adjust_ships_to_ocean(
        self,
        lat1: np.ndarray,
        lon1: np.ndarray,
        lat2: np.ndarray,
        lon2: np.ndarray,
        ang: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        aircraft_count = self.config.aircraft_count
        for local_ship_idx in range(self.config.ship_count):
            i = aircraft_count + local_ship_idx
            lat_deg = float(np.rad2deg(lat2[i]))
            lon_deg = float(np.rad2deg(lon2[i]))
            if not self._is_land(lat_deg, lon_deg):
                continue

            # Redirect ship heading around coastlines when next step lands on ground.
            base_heading = float(self._mobile_heading[i])
            heading_offsets = (
                0.30, -0.30, 0.60, -0.60, 0.90, -0.90, 1.2, -1.2, 1.5, -1.5, np.pi
            )
            moved = False
            for off in heading_offsets:
                new_heading = base_heading + off
                c_lat = np.arcsin(
                    np.sin(lat1[i]) * np.cos(ang[i])
                    + np.cos(lat1[i]) * np.sin(ang[i]) * np.cos(new_heading)
                )
                c_lon = lon1[i] + np.arctan2(
                    np.sin(new_heading) * np.sin(ang[i]) * np.cos(lat1[i]),
                    np.cos(ang[i]) - np.sin(lat1[i]) * np.sin(c_lat),
                )
                c_lon = (c_lon + np.pi) % (2.0 * np.pi) - np.pi
                c_lat_deg = float(np.rad2deg(c_lat))
                c_lon_deg = float(np.rad2deg(c_lon))
                if not self._is_land(c_lat_deg, c_lon_deg):
                    lat2[i] = c_lat
                    lon2[i] = c_lon
                    self._mobile_heading[i] = new_heading
                    moved = True
                    break

            if not moved:
                # If all alternatives fail, hold current position and randomize heading.
                lat2[i] = lat1[i]
                lon2[i] = lon1[i]
                self._mobile_heading[i] = base_heading + float(self._rng.uniform(-np.pi / 2.0, np.pi / 2.0))

        return lat2, lon2

    def _adjacency_from_positions(self, positions: np.ndarray) -> np.ndarray:
        los, dist, delta = self._geometry_matrices_incremental(positions)
        candidate = los & (dist <= self._dmax_matrix)
        np.fill_diagonal(candidate, False)

        beam_ok = self._satellite_beam_mask(positions, delta)
        candidate &= beam_ok

        sat_mandatory = self._build_same_plane_mandatory_edges()
        sat_backbone = self._build_satellite_backbone(candidate, dist)
        candidate_wo_sat_sat = candidate.copy()
        candidate_wo_sat_sat[: self._sat_count, : self._sat_count] = False

        capped = self._apply_capacity_constraints(candidate_wo_sat_sat, dist)
        combined = capped | sat_backbone
        np.fill_diagonal(combined, False)
        stable = self._stabilize_links(combined)
        # Same-plane satellite neighbor links are deterministic backbone edges.
        # Keep them always on instead of delaying via hysteresis counters.
        stable |= sat_mandatory
        np.fill_diagonal(stable, False)
        stable = np.logical_or(stable, stable.T)
        return stable

    def _geometry_matrices_incremental(self, positions: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if not self.config.incremental_geometry:
            los, dist, delta = self._geometry_matrices(positions)
            self._cached_positions = positions.copy()
            self._cached_los = los.copy()
            self._cached_dist = dist.copy()
            self._cached_delta = delta.copy()
            return los, dist, delta

        if (
            self._cached_positions is None
            or self._cached_los is None
            or self._cached_dist is None
            or self._cached_delta is None
            or self._cached_positions.shape != positions.shape
        ):
            los, dist, delta = self._geometry_matrices(positions)
            self._cached_positions = positions.copy()
            self._cached_los = los.copy()
            self._cached_dist = dist.copy()
            self._cached_delta = delta.copy()
            return los, dist, delta

        moved = np.linalg.norm(positions - self._cached_positions, axis=1) > self.config.incremental_move_threshold_m
        affected = np.where(moved)[0]
        n = positions.shape[0]
        if affected.size == 0:
            return self._cached_los, self._cached_dist, self._cached_delta

        ratio = affected.size / max(1, n)
        if ratio >= self.config.incremental_rebuild_ratio:
            los, dist, delta = self._geometry_matrices(positions)
            self._cached_positions = positions.copy()
            self._cached_los = los.copy()
            self._cached_dist = dist.copy()
            self._cached_delta = delta.copy()
            return los, dist, delta

        los = self._cached_los.copy()
        dist = self._cached_dist.copy()
        delta = self._cached_delta.copy()
        earth_sq = EARTH_RADIUS_M * EARTH_RADIUS_M

        for idx in affected.tolist():
            i = int(idx)
            row_delta = positions - positions[i]
            col_delta = positions[i] - positions
            delta[i, :, :] = row_delta
            delta[:, i, :] = col_delta

            seg_len_sq = np.sum(row_delta * row_delta, axis=1)
            seg_len_sq = np.where(seg_len_sq == 0.0, 1.0, seg_len_sq)
            row_dist = np.sqrt(seg_len_sq)

            t = -np.sum(positions[i] * row_delta, axis=1) / seg_len_sq
            t = np.clip(t, 0.0, 1.0)
            closest = positions[i] + t[:, np.newaxis] * row_delta
            closest_sq = np.sum(closest * closest, axis=1)
            row_los = closest_sq > earth_sq
            row_los[i] = False

            dist[i, :] = row_dist
            dist[:, i] = row_dist
            los[i, :] = row_los
            los[:, i] = row_los

        np.fill_diagonal(los, False)
        self._cached_positions = positions.copy()
        self._cached_los = los
        self._cached_dist = dist
        self._cached_delta = delta
        return los, dist, delta

    def _same_plane_sat_neighbors(self, sat_idx: int) -> tuple[int, int]:
        if sat_idx < self._polar_count:
            offset = 0
            planes = self._polar_planes
            sats_per_plane = self._polar_sats_per_plane
            local = sat_idx
        else:
            offset = self._polar_count
            planes = self._incl_planes
            sats_per_plane = self._incl_sats_per_plane
            local = sat_idx - self._polar_count

        if planes <= 0 or sats_per_plane <= 1:
            return sat_idx, sat_idx

        plane = local % planes
        slot = local // planes
        prev_slot = (slot - 1) % sats_per_plane
        next_slot = (slot + 1) % sats_per_plane
        prev_idx = offset + prev_slot * planes + plane
        next_idx = offset + next_slot * planes + plane
        return int(prev_idx), int(next_idx)

    def _build_satellite_backbone(self, candidate: np.ndarray, dist: np.ndarray) -> np.ndarray:
        n = self.config.total_nodes
        selected = np.zeros((n, n), dtype=bool)
        if self._sat_count == 0:
            return selected

        sat_adj = selected[: self._sat_count, : self._sat_count]
        sat_degree = np.zeros(self._sat_count, dtype=np.int16)
        target_deg = int(max(0, self._link_policy.sat_isl_ports))

        mandatory_edges: set[tuple[int, int]] = set()
        for i in range(self._sat_count):
            a, b = self._same_plane_sat_neighbors(i)
            if a != i:
                mandatory_edges.add((min(i, a), max(i, a)))
            if b != i:
                mandatory_edges.add((min(i, b), max(i, b)))

        for i, j in mandatory_edges:
            sat_adj[i, j] = True
            sat_adj[j, i] = True
            sat_degree[i] += 1
            sat_degree[j] += 1

        for _ in range(2):
            for i in range(self._sat_count):
                while sat_degree[i] < target_deg:
                    if self._sat_orbit_code[i] == 0:
                        pool = self._polar_sat_idx
                    else:
                        pool = self._all_sat_idx

                    best_j = -1
                    best_d = np.inf
                    for j in pool:
                        jj = int(j)
                        if jj == i or sat_adj[i, jj]:
                            continue
                        if not candidate[i, jj]:
                            continue
                        if sat_degree[jj] >= target_deg:
                            continue
                        dij = float(dist[i, jj])
                        if dij < best_d:
                            best_d = dij
                            best_j = jj

                    if best_j < 0:
                        break

                    sat_adj[i, best_j] = True
                    sat_adj[best_j, i] = True
                    sat_degree[i] += 1
                    sat_degree[best_j] += 1

        np.fill_diagonal(selected, False)
        return selected

    def _build_same_plane_mandatory_edges(self) -> np.ndarray:
        n = self.config.total_nodes
        selected = np.zeros((n, n), dtype=bool)
        sat_adj = selected[: self._sat_count, : self._sat_count]
        for i in range(self._sat_count):
            a, b = self._same_plane_sat_neighbors(i)
            if a != i:
                sat_adj[i, a] = True
                sat_adj[a, i] = True
            if b != i:
                sat_adj[i, b] = True
                sat_adj[b, i] = True
        np.fill_diagonal(selected, False)
        return selected

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
        mobile_degree = np.zeros(n, dtype=np.int16)
        sat_sat_degree = np.zeros(self._sat_count, dtype=np.int16)

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
            i_is_sat = i < self._sat_count
            j_is_sat = j < self._sat_count

            # ISL links are limited by hardware ports on each satellite.
            if i_is_sat and j_is_sat:
                if sat_sat_degree[i] >= self._link_policy.sat_isl_ports:
                    continue
                if sat_sat_degree[j] >= self._link_policy.sat_isl_ports:
                    continue
            # Mobile-mobile links keep degree caps to avoid dense local meshes.
            elif not i_is_sat and not j_is_sat:
                if mobile_degree[i] >= self._degree_caps[i]:
                    continue
                if mobile_degree[j] >= self._degree_caps[j]:
                    continue
            # Satellite-mobile links are intentionally not capped by count.

            selected[i, j] = True
            selected[j, i] = True

            if i_is_sat and j_is_sat:
                sat_sat_degree[i] += 1
                sat_sat_degree[j] += 1
            elif not i_is_sat and not j_is_sat:
                mobile_degree[i] += 1
                mobile_degree[j] += 1

        np.fill_diagonal(selected, False)
        return selected

    def _stabilize_links(self, candidate: np.ndarray) -> np.ndarray:
        prev = self._adj_prev

        self._up_count = np.where(~prev & candidate, np.minimum(self._up_count + 1, 255), 0).astype(np.uint8)
        self._down_count = np.where(prev & ~candidate, np.minimum(self._down_count + 1, 255), 0).astype(np.uint8)
        age_next = self._state_age_ticks.astype(np.uint32) + 1
        self._state_age_ticks = np.minimum(age_next, 65535).astype(np.uint16)

        new_adj = prev.copy()
        promote = (
            (~prev)
            & (self._up_count >= self._up_hold_ticks)
            & (self._state_age_ticks >= self._min_link_down_ticks)
        )
        demote = (
            prev
            & (self._down_count >= self._down_hold_ticks)
            & (self._state_age_ticks >= self._min_link_up_ticks)
        )

        new_adj[promote] = True
        new_adj[demote] = False

        flips = promote | demote
        self._last_flip_count = int(np.count_nonzero(np.triu(flips, k=1)))
        self._state_age_ticks[flips] = 0

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

    def _connected_components_summary(self, adjacency: np.ndarray) -> tuple[int, int, np.ndarray]:
        n = adjacency.shape[0]
        visited = np.zeros(n, dtype=bool)
        comp_count = 0
        largest_size = 0
        largest_nodes = np.array([], dtype=np.int32)

        for s in range(n):
            if visited[s]:
                continue
            comp_count += 1
            stack = [s]
            visited[s] = True
            nodes: list[int] = []
            while stack:
                u = stack.pop()
                nodes.append(u)
                nbrs = np.where(adjacency[u])[0]
                for v in nbrs:
                    vv = int(v)
                    if not visited[vv]:
                        visited[vv] = True
                        stack.append(vv)
            if len(nodes) > largest_size:
                largest_size = len(nodes)
                largest_nodes = np.array(nodes, dtype=np.int32)
        return comp_count, largest_size, largest_nodes

    def _approx_component_diameter(self, adjacency: np.ndarray, nodes: np.ndarray) -> int:
        if nodes.size <= 1:
            return 0

        node_set = set(int(x) for x in nodes.tolist())

        def bfs_farthest(src: int) -> tuple[int, int]:
            dist = {src: 0}
            q = [src]
            head = 0
            far = src
            while head < len(q):
                u = q[head]
                head += 1
                du = dist[u]
                if du > dist[far]:
                    far = u
                for v in np.where(adjacency[u])[0]:
                    vv = int(v)
                    if vv not in node_set or vv in dist:
                        continue
                    dist[vv] = du + 1
                    q.append(vv)
            return far, dist[far]

        start = int(nodes[0])
        a, _ = bfs_farthest(start)
        _, diam = bfs_farthest(a)
        return diam


def estimated_working_set_mb(node_count: int = 300) -> float:
    # Main NxN tensors in adjacency computation: delta(3), t(1), closest(3), los(1), dist(1).
    bytes_per_pair = (3 + 1 + 3 + 1) * 8 + 1
    total_bytes = node_count * node_count * bytes_per_pair
    return total_bytes / (1024 * 1024)
