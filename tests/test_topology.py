from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pytest

from dynamic_topo.engine import (
    EARTH_RADIUS_M,
    NODE_TYPE_AIR,
    NODE_TYPE_LEO,
    SimulationConfig,
    TopologyEngine,
    estimated_working_set_mb,
)
from dynamic_topo.storage import InMemoryRedis
from scripts.generate_topology_snapshot import build_snapshot


def build_engine() -> TopologyEngine:
    cfg = SimulationConfig()
    return TopologyEngine(cfg, seed=7, redis_client=InMemoryRedis())


def test_node_counts_and_names() -> None:
    engine = build_engine()
    ids = engine.node_ids

    assert len(ids) == 300
    assert ids[0] == "SAT-POLAR-001"
    assert ids[99] == "SAT-POLAR-100"
    assert ids[100] == "SAT-INCL-001"
    assert ids[199] == "SAT-INCL-100"
    assert ids[200] == "AIR-001"
    assert ids[249] == "AIR-050"
    assert ids[250] == "SHIP-001"
    assert ids[299] == "SHIP-050"


def test_1hz_step_progression() -> None:
    engine = build_engine()
    results = engine.run_steps(steps=4, start_time_s=0.0)
    times = [r.sim_time_s for r in results]

    assert times == [0.0, 1.0, 2.0, 3.0]


def test_topology_matrix_is_symmetric_and_diagonal_zero() -> None:
    engine = build_engine()
    result = engine.step(0.0)
    adj = result.adjacency

    assert adj.shape == (300, 300)
    assert np.array_equal(adj, adj.T)
    assert not np.any(np.diag(adj))


def test_satellite_satellite_degree_respects_isl_ports() -> None:
    engine = build_engine()
    # Two steps to pass 2s hysteresis and expose active links.
    engine.step(0.0)
    result = engine.step(1.0)

    sat_adj = result.adjacency[:200, :200]
    sat_sat_degree = sat_adj.sum(axis=1)
    assert np.all(sat_sat_degree <= engine.config.sat_isl_ports)


def test_satellite_keeps_same_plane_neighbors() -> None:
    engine = build_engine()
    engine.step(0.0)
    result = engine.step(1.0)

    sat_adj = result.adjacency[:200, :200]
    for i in range(200):
        a, b = engine._same_plane_sat_neighbors(i)
        if a != i:
            assert bool(sat_adj[i, a])
        if b != i:
            assert bool(sat_adj[i, b])


def test_satellite_same_plane_neighbors_are_on_from_first_tick() -> None:
    engine = build_engine()
    result = engine.step(0.0)
    sat_adj = result.adjacency[:200, :200]
    for i in range(200):
        a, b = engine._same_plane_sat_neighbors(i)
        if a != i:
            assert bool(sat_adj[i, a])
        if b != i:
            assert bool(sat_adj[i, b])


def test_satellite_mobile_edges_are_not_capped_by_count() -> None:
    cfg = SimulationConfig(
        total_nodes=7,
        leo_polar_count=1,
        leo_inclined_count=0,
        aircraft_count=6,
        ship_count=0,
        sat_isl_ports=4,
    )
    engine = TopologyEngine(cfg, seed=1, redis_client=InMemoryRedis())

    candidate = np.zeros((7, 7), dtype=bool)
    for j in range(1, 7):
        candidate[0, j] = True
        candidate[j, 0] = True
    dist = np.ones((7, 7), dtype=np.float64)

    selected = engine._apply_capacity_constraints(candidate, dist)
    assert int(selected[0].sum()) == 6


def test_satellite_beam_rejects_far_off_nadir_target() -> None:
    cfg = SimulationConfig(
        total_nodes=2,
        leo_polar_count=1,
        leo_inclined_count=0,
        aircraft_count=1,
        ship_count=0,
        sat_beam_half_angle_deg=30.0,
    )
    engine = TopologyEngine(cfg, seed=1, redis_client=InMemoryRedis())

    sat = np.array([EARTH_RADIUS_M + cfg.leo_altitude_m, 0.0, 0.0])
    target_off_nadir = np.array([0.0, EARTH_RADIUS_M + cfg.aircraft_altitude_m, 0.0])
    positions = np.vstack([sat, target_off_nadir])

    _, _, delta = engine._geometry_matrices(positions)
    beam = engine._satellite_beam_mask(positions, delta)
    assert not bool(beam[0, 1])


def test_hysteresis_needs_two_ticks_for_up_and_down() -> None:
    cfg = SimulationConfig(
        total_nodes=2,
        leo_polar_count=1,
        leo_inclined_count=0,
        aircraft_count=1,
        ship_count=0,
        up_hold_s=2.0,
        down_hold_s=2.0,
        timestep_s=1.0,
    )
    engine = TopologyEngine(cfg, seed=1, redis_client=InMemoryRedis())

    candidate_up = np.array([[False, True], [True, False]], dtype=bool)
    candidate_down = np.array([[False, False], [False, False]], dtype=bool)

    a1 = engine._stabilize_links(candidate_up)
    a2 = engine._stabilize_links(candidate_up)
    a3 = engine._stabilize_links(candidate_down)
    a4 = engine._stabilize_links(candidate_down)

    assert not a1[0, 1]
    assert a2[0, 1]
    assert a3[0, 1]
    assert not a4[0, 1]


def test_tick_completes_within_100ms_without_network_io() -> None:
    engine = build_engine()
    result = engine.step(0.0)

    assert result.elapsed_ms < 100.0


def test_estimated_memory_below_512mb() -> None:
    assert estimated_working_set_mb(300) < 512.0


def test_node_types_are_mapped_correctly() -> None:
    engine = build_engine()
    assert np.all(engine._type_codes[:200] == NODE_TYPE_LEO)
    assert np.all(engine._type_codes[200:250] == NODE_TYPE_AIR)


def test_leo_orbit_groups_have_expected_inclinations() -> None:
    engine = build_engine()
    incl = engine._sat_inclinations_deg
    assert np.allclose(incl[:100], 97.6)
    assert np.allclose(incl[100:200], 53.0)


def test_satellite_positions_are_not_collapsed_at_epoch() -> None:
    engine = build_engine()
    result = engine.step(0.0)
    sat = result.node_positions_ecef[:200]
    # Round to meter-level and require wide spread to catch accidental orbital collapse.
    rounded = np.round(sat, 0)
    unique = np.unique(rounded, axis=0)
    assert unique.shape[0] > 180


def test_build_frame_contains_nodes_links_and_metrics() -> None:
    engine = build_engine()
    engine.step(0.0)  # warm hysteresis
    result = engine.step(1.0)
    frame = engine.build_frame(result)

    assert frame.sim_time_s == 1.0
    assert len(frame.nodes) == 300
    assert "edge_count" in frame.metrics
    assert "avg_degree" in frame.metrics
    assert "max_degree" in frame.metrics
    assert "link_flip_count_tick" in frame.metrics
    assert "mobile_connected_count" in frame.metrics
    assert "mobile_connected_ratio" in frame.metrics
    assert 0.0 <= frame.metrics["mobile_connected_ratio"] <= 1.0
    assert all("id" in node and "lat" in node and "lon" in node for node in frame.nodes)
    assert frame.nodes[0]["orbit_class"] == "polar"
    assert frame.nodes[100]["orbit_class"] == "inclined"
    assert frame.nodes[200]["category"] == "aircraft"
    assert frame.nodes[0]["vx"] is not None


def test_ships_remain_on_ocean_mask_for_multiple_steps() -> None:
    engine = build_engine()
    for t in range(0, 60, 5):
        result = engine.step(float(t))
        frame = engine.build_frame(result)
        for node in frame.nodes[250:300]:
            assert not engine._is_land(node["lat"], node["lon"])


def test_link_policy_file_override_is_applied(tmp_path) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "dmax_air_ship_m": 123456.0,
                "max_neighbors_air": 2,
                "sat_isl_ports": 3,
            }
        ),
        encoding="utf-8",
    )
    cfg = SimulationConfig(link_policy_path=str(policy_path))
    engine = TopologyEngine(cfg, seed=7, redis_client=InMemoryRedis())

    assert engine._link_policy.dmax_air_ship_m == 123456.0
    assert engine._link_policy.max_neighbors_air == 2
    assert engine._link_policy.sat_isl_ports == 3


def test_link_policy_file_rejects_unknown_keys(tmp_path) -> None:
    policy_path = tmp_path / "bad_policy.json"
    policy_path.write_text(json.dumps({"not_exist_key": 1}), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown link policy keys"):
        _ = TopologyEngine(
            SimulationConfig(link_policy_path=str(policy_path)),
            seed=7,
            redis_client=InMemoryRedis(),
        )


def test_link_policy_hot_reload_updates_runtime(tmp_path) -> None:
    policy_path = tmp_path / "policy_hot.json"
    policy_path.write_text(json.dumps({"sat_isl_ports": 2}), encoding="utf-8")
    cfg = SimulationConfig(link_policy_path=str(policy_path), link_policy_hot_reload=True)
    engine = TopologyEngine(cfg, seed=7, redis_client=InMemoryRedis())

    assert engine._link_policy.sat_isl_ports == 2
    policy_path.write_text(json.dumps({"sat_isl_ports": 5}), encoding="utf-8")

    # Trigger a step so the engine checks the file mtime and reloads policy.
    _ = engine.step(0.0, persist=False)
    assert engine._link_policy.sat_isl_ports == 5


def test_min_link_up_hold_blocks_early_demote() -> None:
    cfg = SimulationConfig(
        total_nodes=2,
        leo_polar_count=1,
        leo_inclined_count=0,
        aircraft_count=1,
        ship_count=0,
        up_hold_s=1.0,
        down_hold_s=1.0,
        min_link_up_s=3.0,
        timestep_s=1.0,
    )
    engine = TopologyEngine(cfg, seed=1, redis_client=InMemoryRedis())
    candidate_up = np.array([[False, True], [True, False]], dtype=bool)
    candidate_down = np.array([[False, False], [False, False]], dtype=bool)

    up = engine._stabilize_links(candidate_up)
    down1 = engine._stabilize_links(candidate_down)
    down2 = engine._stabilize_links(candidate_down)
    down3 = engine._stabilize_links(candidate_down)

    assert up[0, 1]
    assert down1[0, 1]
    assert down2[0, 1]
    assert not down3[0, 1]


def test_min_link_down_hold_blocks_early_promote() -> None:
    cfg = SimulationConfig(
        total_nodes=2,
        leo_polar_count=1,
        leo_inclined_count=0,
        aircraft_count=1,
        ship_count=0,
        up_hold_s=1.0,
        down_hold_s=1.0,
        min_link_down_s=2.0,
        timestep_s=1.0,
    )
    engine = TopologyEngine(cfg, seed=1, redis_client=InMemoryRedis())
    candidate_up = np.array([[False, True], [True, False]], dtype=bool)
    candidate_down = np.array([[False, False], [False, False]], dtype=bool)

    _ = engine._stabilize_links(candidate_up)
    down = engine._stabilize_links(candidate_down)
    up1 = engine._stabilize_links(candidate_up)
    up2 = engine._stabilize_links(candidate_up)

    assert not down[0, 1]
    assert not up1[0, 1]
    assert up2[0, 1]


def test_topology_snapshot_matches_baseline() -> None:
    baseline_path = Path("tests/fixtures/topology_snapshot.json")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    current = build_snapshot(
        steps=int(baseline["meta"]["steps"]),
        dt=float(baseline["meta"]["dt"]),
        seed=int(baseline["meta"]["seed"]),
    )
    assert current == baseline
