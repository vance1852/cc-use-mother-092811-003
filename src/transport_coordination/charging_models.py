"""干线充电保障服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StationEquipment:
    """站点在某一时刻的设备快照。"""

    station_id: str
    name: str
    queue_timeout_minutes: int
    chargers: tuple[dict[str, Any], ...]
    power_schedule: tuple[dict[str, Any], ...]
    digest: str
    generated_at: str


@dataclass(frozen=True)
class RoadNetwork:
    """某个通行版本下的干线拓扑。"""

    version: int
    note: str
    nodes: dict[str, dict[str, Any]]
    segments: dict[str, dict[str, Any]]
    open_segment_ids: frozenset[str]
    generated_at: str


@dataclass(frozen=True)
class PlanStopView:
    """补能计划中的单站描述。"""

    seq: int
    station_id: str
    station_name: str
    arrive_at: str
    depart_at: str
    slot_start: str
    slot_end: str
    charge_kwh: float
    planned_power_kw: float
    arrive_range_km: float
    reserve_margin_km: float


@dataclass(frozen=True)
class PlanView:
    """一趟运输的一版补能计划。"""

    plan_id: str
    trip_id: str
    version_seq: int
    status: str
    feasible: bool
    road_version: int
    valid_from: str
    valid_until: str
    created_at: str
    infeasible_reason: str | None
    stops: tuple[PlanStopView, ...] = field(default_factory=tuple)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReservationStopView:
    """一个已锁定站点时隙的运行状态。"""

    seq: int
    station_id: str
    status: str
    slot_start: str
    slot_end: str
    arrive_at: str | None
    depart_at: str | None
    charge_kwh: float
    delivered_kwh: float
    is_fact: bool


@dataclass(frozen=True)
class ReservationView:
    """一次司机确认形成的多站锁定结果。"""

    reservation_id: str
    trip_id: str
    plan_id: str
    status: str
    stops: tuple[ReservationStopView, ...]


@dataclass(frozen=True)
class ReplanView:
    """一次改派的原因与新旧计划对照。"""

    replan_id: str
    trip_id: str
    seq: int
    reason: str
    detail: dict[str, Any]
    from_plan_id: str | None
    to_plan_id: str | None
    created_at: str
