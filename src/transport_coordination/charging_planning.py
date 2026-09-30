"""在道路快照与设备快照上做补能规划的纯函数。

时间统一使用带时区的 UTC datetime，分时功率按一天内的分钟数描述。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

SLOT_MINUTES = 15
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES
EPS = 1e-6


@dataclass
class RoutePoint:
    """路径上的一个节点。"""

    node_id: str
    station_id: str | None
    distance_km: float
    travel_minutes: float


@dataclass
class PlannedStop:
    """规划器内部产出的单站补能安排。"""

    seq: int
    station_id: str
    station_name: str
    node_id: str
    arrive_at: datetime
    charge_start: datetime
    depart_at: datetime
    charge_kwh: float
    planned_power_kw: float
    arrive_range_km: float
    reserve_margin_km: float
    slots: tuple[datetime, ...] = field(default_factory=tuple)


@dataclass
class PlanResult:
    """一次规划尝试的结果。"""

    feasible: bool
    stops: list[PlannedStop] = field(default_factory=list)
    reason: str | None = None
    unusable_station: str | None = None
    destination_arrive_at: datetime | None = None
    destination_margin_km: float = 0.0
    total_charge_kwh: float = 0.0
    total_wait_minutes: float = 0.0


# --------------------------------------------------------------------- 路网

def build_directed_edges(network: Any) -> dict[str, list[tuple[str, str, float, float]]]:
    """把通行版本中的道路展开为有向边，封闭段不出现。"""

    edges: dict[str, list[tuple[str, str, float, float]]] = {}
    for segment in network.segments.values():
        if not segment["open"]:
            continue
        edges.setdefault(segment["from_node"], []).append(
            (segment["segment_id"], segment["to_node"],
             float(segment["distance_km"]), float(segment["speed_kmh"])))
        if segment["bidirectional"]:
            edges.setdefault(segment["to_node"], []).append(
                (segment["segment_id"], segment["from_node"],
                 float(segment["distance_km"]), float(segment["speed_kmh"])))
    return edges


def shortest_route(network: Any, origin: str, destination: str) -> list[RoutePoint]:
    """在当前开放道路上按距离做最短路，返回带累计里程/时间的节点序列。"""

    if origin not in network.nodes:
        raise ValueError("起点节点不在当前道路版本中")
    if destination not in network.nodes:
        raise ValueError("终点节点不在当前道路版本中")
    edges = build_directed_edges(network)
    dist: dict[str, float] = {origin: 0.0}
    time_min: dict[str, float] = {origin: 0.0}
    previous: dict[str, tuple[str, float, float]] = {}
    queue: list[tuple[float, str]] = [(0.0, origin)]
    while queue:
        accumulated, node = heapq.heappop(queue)
        if accumulated > dist.get(node, math.inf) + EPS:
            continue
        if node == destination:
            break
        for _, to_node, distance, speed in edges.get(node, ()):
            candidate = accumulated + distance
            if candidate + EPS < dist.get(to_node, math.inf):
                dist[to_node] = candidate
                time_min[to_node] = time_min[node] + distance / speed * 60.0
                previous[to_node] = (node, distance, distance / speed * 60.0)
                heapq.heappush(queue, (candidate, to_node))
    if destination not in dist:
        return []
    ordered = [destination]
    while ordered[-1] != origin:
        ordered.append(previous[ordered[-1]][0])
    ordered.reverse()
    return [RoutePoint(node_id, network.nodes[node_id].get("station_id"),
                       dist[node_id], time_min[node_id]) for node_id in ordered]


# --------------------------------------------------------------------- 时间

def slot_floor(moment: datetime) -> datetime:
    """向下取整到 15 分钟时隙起点。"""

    minute = (moment.minute // SLOT_MINUTES) * SLOT_MINUTES
    return moment.replace(minute=minute, second=0, microsecond=0)


def slot_range(start: datetime, end: datetime) -> tuple[datetime, ...]:
    """覆盖 [start, end) 的全部时隙起点。"""

    slots = []
    cursor = slot_floor(start)
    while cursor < end:
        slots.append(cursor)
        cursor += timedelta(minutes=SLOT_MINUTES)
    return tuple(slots)


# --------------------------------------------------------------------- 规划

CapacityFn = Callable[[str, datetime], dict[str, float]]


def _vehicle_power(equipment: Any, vehicle_max_kw: float) -> float:
    operational = [c["max_power_kw"] for c in equipment.chargers if c["status"] == "operational"]
    if not operational:
        return 0.0
    return min(vehicle_max_kw, max(operational))


def _slot_available(station_id: str, slot: datetime, power_kw: float,
                    capacity_fn: CapacityFn) -> bool:
    capacity = capacity_fn(station_id, slot)
    if capacity["used_chargers"] + 1 > capacity["cap_chargers"] + EPS:
        return False
    if capacity["used_power"] + power_kw > capacity["cap_power"] + EPS:
        return False
    return True


def _find_window(equipment: Any, arrive_at: datetime, energy_kwh: float,
                 vehicle_max_kw: float, capacity_fn: CapacityFn
                 ) -> tuple[datetime, datetime, float, tuple[datetime, ...]] | None:
    """在排队超时窗口内逐 15 分钟向后寻找连续可用的充电时隙组合。"""

    power = _vehicle_power(equipment, vehicle_max_kw)
    if power <= 0:
        return None
    duration_slots = max(1, math.ceil(energy_kwh / power * 60.0 / SLOT_MINUTES))
    duration = timedelta(minutes=duration_slots * SLOT_MINUTES)
    timeout = timedelta(minutes=equipment.queue_timeout_minutes)
    start = slot_floor(arrive_at)
    latest_start = slot_floor(arrive_at + timeout)
    while start <= latest_start:
        end = start + duration
        slots = slot_range(start, end)
        if all(_slot_available(equipment.station_id, slot, power, capacity_fn) for slot in slots):
            return start, end, power, slots
        start += timedelta(minutes=SLOT_MINUTES)
    return None


def make_plan(*, route: list[RoutePoint], station_equipment: dict[str, Any],
              initial_energy_kwh: float, battery_kwh: float, max_charge_kw: float,
              rate_kwh_per_km: float, reserve_km: float, depart_at: datetime,
              arrive_by: datetime, capacity_fn: CapacityFn,
              station_names: dict[str, str],
              excluded: frozenset[str] = frozenset()) -> PlanResult:
    """沿固定路径按最小补能贪心生成计划；excluded 中的站点不作为补能点。"""

    if not route:
        return PlanResult(False, reason="当前道路版本下起点到终点不可达")
    stations = [(i, p) for i, p in enumerate(route)
                if p.station_id and p.station_id not in excluded]
    destination = route[-1]
    total_charge = 0.0
    clock_offset_minutes = 0.0
    stops: list[PlannedStop] = []

    for order, (_, point) in enumerate(stations):
        arrive_at = depart_at + timedelta(minutes=point.travel_minutes + clock_offset_minutes)
        arrive_energy = initial_energy_kwh + total_charge - point.distance_km * rate_kwh_per_km
        arrive_range_km = arrive_energy / rate_kwh_per_km
        if arrive_range_km + EPS < reserve_km:
            return PlanResult(False, reason=f"到达站点 {point.station_id} 前续航低于安全余量")
        later = stations[order + 1:]
        target = later[0][1] if later else destination
        target_distance = target.distance_km - point.distance_km
        need_to_target = (target_distance + reserve_km) * rate_kwh_per_km
        current_energy_at_station = arrive_energy
        if current_energy_at_station + EPS >= need_to_target:
            continue
        # 即便在本站充满也无法在保留安全余量的前提下抵达下一补能点
        if battery_kwh + EPS < need_to_target:
            return PlanResult(False,
                              reason=f"站点 {point.station_id} 之后的站间距超出满电续航")
        charge_kwh = min(need_to_target - current_energy_at_station,
                         battery_kwh - current_energy_at_station)
        equipment = station_equipment.get(point.station_id)
        if equipment is None:
            return PlanResult(False, reason=f"站点 {point.station_id} 缺少设备资料",
                              unusable_station=point.station_id)
        window = _find_window(equipment, arrive_at, charge_kwh, max_charge_kw, capacity_fn)
        if window is None:
            return PlanResult(False, reason=f"站点 {point.station_id} 在可排队窗口内没有足够容量",
                              unusable_station=point.station_id)
        charge_start, depart, power, _ = window
        dwell_minutes = (depart - arrive_at).total_seconds() / 60.0
        clock_offset_minutes += dwell_minutes
        total_charge += charge_kwh
        stops.append(PlannedStop(
            seq=len(stops) + 1, station_id=point.station_id,
            station_name=station_names.get(point.station_id, point.station_id),
            node_id=point.node_id, arrive_at=arrive_at, charge_start=charge_start,
            depart_at=depart, charge_kwh=charge_kwh, planned_power_kw=power,
            arrive_range_km=arrive_range_km,
            reserve_margin_km=arrive_range_km - reserve_km))

    final_energy = initial_energy_kwh + total_charge - destination.distance_km * rate_kwh_per_km
    final_margin_km = final_energy / rate_kwh_per_km - reserve_km
    if final_margin_km + EPS < 0:
        return PlanResult(False, reason="抵达终点时续航低于安全余量")
    destination_arrival = depart_at + timedelta(
        minutes=destination.travel_minutes + clock_offset_minutes)
    if destination_arrival > arrive_by:
        return PlanResult(False, reason="按当前功率与排队情况无法在任务时限前抵达")
    return PlanResult(True, stops=stops, destination_arrive_at=destination_arrival,
                      destination_margin_km=final_margin_km, total_charge_kwh=total_charge,
                      total_wait_minutes=clock_offset_minutes - sum(
                          (s.depart_at - s.charge_start).total_seconds() / 60.0 for s in stops))


def plan_with_fallback(**kwargs: Any) -> PlanResult:
    """反复排除无容量/不可用站点后重试，直到可行或确认无可行解。"""

    excluded: set[str] = set()
    while True:
        result = make_plan(excluded=frozenset(excluded), **kwargs)
        if result.feasible or not result.unusable_station:
            return result
        if result.unusable_station in excluded:
            return result
        excluded.add(result.unusable_station)
