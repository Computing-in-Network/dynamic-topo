from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LinkPolicy:
    dmax_leo_leo_m: float
    dmax_leo_air_m: float
    dmax_leo_ship_m: float
    dmax_air_air_m: float
    dmax_air_ship_m: float
    dmax_ship_ship_m: float
    max_neighbors_leo: int
    sat_isl_ports: int
    max_neighbors_air: int
    max_neighbors_ship: int
    sat_beam_half_angle_deg: float
    sat_beam_slots: int
    up_hold_s: float
    down_hold_s: float
    min_link_up_s: float
    min_link_down_s: float

    @classmethod
    def from_simulation_config(cls, cfg: Any) -> "LinkPolicy":
        return cls(
            dmax_leo_leo_m=float(cfg.dmax_leo_leo_m),
            dmax_leo_air_m=float(cfg.dmax_leo_air_m),
            dmax_leo_ship_m=float(cfg.dmax_leo_ship_m),
            dmax_air_air_m=float(cfg.dmax_air_air_m),
            dmax_air_ship_m=float(cfg.dmax_air_ship_m),
            dmax_ship_ship_m=float(cfg.dmax_ship_ship_m),
            max_neighbors_leo=int(cfg.max_neighbors_leo),
            sat_isl_ports=int(cfg.sat_isl_ports),
            max_neighbors_air=int(cfg.max_neighbors_air),
            max_neighbors_ship=int(cfg.max_neighbors_ship),
            sat_beam_half_angle_deg=float(cfg.sat_beam_half_angle_deg),
            sat_beam_slots=int(cfg.sat_beam_slots),
            up_hold_s=float(cfg.up_hold_s),
            down_hold_s=float(cfg.down_hold_s),
            min_link_up_s=float(cfg.min_link_up_s),
            min_link_down_s=float(cfg.min_link_down_s),
        )

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def load_link_policy_file(path: str, base: LinkPolicy) -> LinkPolicy:
    policy_path = Path(path)
    if not policy_path.exists():
        raise ValueError(f"link policy file not found: {path}")
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in link policy file: {path}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"link policy must be a JSON object: {path}")

    allowed = set(base.to_dict().keys())
    unknown = sorted(set(raw.keys()) - allowed)
    if unknown:
        raise ValueError(f"unknown link policy keys: {', '.join(unknown)}")

    updates: dict[str, Any] = {}
    for key, value in raw.items():
        updates[key] = _normalize_value(key, value)

    candidate = replace(base, **updates)
    _validate_policy(candidate)
    return candidate


def _normalize_value(key: str, value: Any) -> Any:
    int_fields = {
        "max_neighbors_leo",
        "sat_isl_ports",
        "max_neighbors_air",
        "max_neighbors_ship",
        "sat_beam_slots",
    }
    float_fields = {
        "dmax_leo_leo_m",
        "dmax_leo_air_m",
        "dmax_leo_ship_m",
        "dmax_air_air_m",
        "dmax_air_ship_m",
        "dmax_ship_ship_m",
        "sat_beam_half_angle_deg",
        "up_hold_s",
        "down_hold_s",
        "min_link_up_s",
        "min_link_down_s",
    }

    if key in int_fields:
        if isinstance(value, bool):
            raise ValueError(f"invalid integer value for {key}")
        return int(value)
    if key in float_fields:
        if isinstance(value, bool):
            raise ValueError(f"invalid float value for {key}")
        return float(value)
    return value


def _validate_policy(policy: LinkPolicy) -> None:
    positive_distances = (
        policy.dmax_leo_leo_m,
        policy.dmax_leo_air_m,
        policy.dmax_leo_ship_m,
        policy.dmax_air_air_m,
        policy.dmax_air_ship_m,
        policy.dmax_ship_ship_m,
    )
    if any(v <= 0.0 for v in positive_distances):
        raise ValueError("all dmax_* values must be > 0")

    if policy.max_neighbors_leo < 0 or policy.max_neighbors_air < 0 or policy.max_neighbors_ship < 0:
        raise ValueError("max_neighbors_* values must be >= 0")
    if policy.sat_isl_ports < 0:
        raise ValueError("sat_isl_ports must be >= 0")
    if policy.sat_beam_slots < 0:
        raise ValueError("sat_beam_slots must be >= 0")
    if not (0.0 < policy.sat_beam_half_angle_deg < 90.0):
        raise ValueError("sat_beam_half_angle_deg must be in (0, 90)")
    if policy.up_hold_s < 0.0 or policy.down_hold_s < 0.0:
        raise ValueError("up_hold_s and down_hold_s must be >= 0")
    if policy.min_link_up_s < 0.0 or policy.min_link_down_s < 0.0:
        raise ValueError("min_link_up_s and min_link_down_s must be >= 0")
