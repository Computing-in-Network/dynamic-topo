from __future__ import annotations

from dynamic_topo.engine import SimulationConfig, TopologyEngine
from dynamic_topo.storage import InMemoryRedis
from dynamic_topo.stream_server import _handle_control_message


def _build_engine() -> TopologyEngine:
    cfg = SimulationConfig(
        total_nodes=4,
        leo_polar_count=2,
        leo_inclined_count=0,
        aircraft_count=2,
        ship_count=0,
    )
    return TopologyEngine(cfg, seed=7, redis_client=InMemoryRedis())


def test_inject_node_fault_and_list_faults() -> None:
    engine = _build_engine()
    ack = _handle_control_message(
        engine,
        {"action": "inject_node_fault", "node_id": "AIR-001", "request_id": "r1"},
    )
    assert ack["ok"] is True
    assert ack["action"] == "inject_node_fault"
    assert ack["request_id"] == "r1"
    assert ack["deduplicated"] is False
    assert ack["fault"]["fault_type"] == "DAMAGED"

    listed = _handle_control_message(engine, {"action": "list_faults"})
    assert listed["ok"] is True
    assert len(listed["faults"]) == 1


def test_duplicate_injection_is_deduplicated() -> None:
    engine = _build_engine()
    a1 = _handle_control_message(engine, {"action": "inject_node_fault", "node_id": "AIR-001"})
    a2 = _handle_control_message(engine, {"action": "inject_node_fault", "node_id": "AIR-001"})
    assert a1["ok"] is True
    assert a2["ok"] is True
    assert a2["deduplicated"] is True
    assert len(engine.list_faults()) == 1


def test_inject_link_fault_and_clear() -> None:
    engine = _build_engine()
    ack = _handle_control_message(
        engine,
        {"action": "inject_link_fault", "a": "SAT-POLAR-001", "b": "AIR-001"},
    )
    assert ack["ok"] is True
    fault_id = ack["fault"]["fault_id"]

    cleared = _handle_control_message(engine, {"action": "clear_fault", "fault_id": fault_id})
    assert cleared["ok"] is True
    assert cleared["faults"] == []


def test_control_message_validation_errors() -> None:
    engine = _build_engine()
    missing = _handle_control_message(engine, {"action": "inject_node_fault"})
    assert missing["ok"] is False
    assert "node_id" in missing["error"]

    unknown = _handle_control_message(engine, {"action": "not_exist_action"})
    assert unknown["ok"] is False
    assert "unknown action" in unknown["error"]

    bad_clear = _handle_control_message(engine, {"action": "clear_fault", "fault_id": "fault-xxx"})
    assert bad_clear["ok"] is False
    assert "fault not found" in bad_clear["error"]

