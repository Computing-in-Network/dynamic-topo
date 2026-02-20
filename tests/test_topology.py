from __future__ import annotations

import numpy as np

from dynamic_topo.engine import (
    EARTH_RADIUS_M,
    NODE_TYPE_AIR,
    NODE_TYPE_LEO,
    SimulationConfig,
    TopologyEngine,
    estimated_working_set_mb,
)
from dynamic_topo.storage import InMemoryRedis


def build_engine() -> TopologyEngine:
    cfg = SimulationConfig()
    return TopologyEngine(cfg, seed=7, redis_client=InMemoryRedis())


def test_node_counts_and_names() -> None:
    engine = build_engine()
    ids = engine.node_ids

    assert len(ids) == 300
    assert ids[0] == "L1-000"
    assert ids[99] == "L1-099"
    assert ids[100] == "L2-000"
    assert ids[199] == "L2-099"
    assert ids[200] == "A1-000"
    assert ids[249] == "A1-049"
    assert ids[250] == "S1-000"
    assert ids[299] == "S1-049"


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


def test_degree_caps_respected() -> None:
    engine = build_engine()
    # Two steps to pass 2s hysteresis and expose active links.
    engine.step(0.0)
    result = engine.step(1.0)

    degrees = result.adjacency.sum(axis=1)
    caps = engine._degree_caps  # internal contract for capacity enforcement
    assert np.all(degrees <= caps)


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
