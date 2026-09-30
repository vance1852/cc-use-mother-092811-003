"""干线充电保障的纯领域逻辑：能量模型、分时功率与补能规划。

本模块不接触数据库与 HTTP，所有输入都是只读快照，方便单测与离线复算。
时间统一使用带时区的 UTC datetime，由服务层负责 ISO 文本转换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

SLOT_MINUTES = 15
SLOT_HOURS = SLOT_MINUTES / 60.0
RESERVE_SAFETY_KM = 0.0


def floor_slot(moment: datetime) -> datetime:
    """向下取整到 15 分钟时隙边界。"""

    minute = moment.minute - moment.minute % SLOT_MINUTES
    return moment.replace(minute=minute, second=0, microsecond=0)


def ceil_slot(moment: datetime) -> datetime:
    """向上取整到 15 分钟时隙边界（恰好落在边界时保持不变）。"""

    floored = floor_slot(moment)
    return floored if floored == moment else floored + timedelta(minutes=SLOT_MINUTES)


def iter_slots(start: datetime, end: datetime):
    """枚举覆盖 [start, end) 的全部时隙起点。"""

    current = start
    while current < end:
        yield current
        current += timedelta(minutes=SLOT_MINUTES)


@dataclass(frozen=True)
class ChargerSpec:
    charger_id: str
    rated_power_kw: float
    active: bool


@dataclass(frozen=True)
class Outage:
    starts_at: datetime
    ends_at: datetime

    def covers(self, moment: datetime) -> bool:
        return self.starts_at <= moment < self.ends_at


@dataclass(frozen=True)
class PowerWindow:
    starts_at: datetime
    ends_at: datetime
    cap_kw: float

    def covers(self, moment: datetime) -> bool:
        return self.starts_at <= moment < self.ends_at


@dataclass(frozen=True)
class RoadSegment:
    from_km: float
    to_km: float
    open_int: int
    detour_extra_km: float

    @property
    def open(self) -> bool:
        return bool(self.open_int)


@dataclass(frozen=True)
class StationSpec:
    station_id: str
    position_km: float
    active: bool
    chargers: tuple[ChargerSpec, ...]
    station_outages: tuple[Outage, ...]
    charger_outages: tuple[tuple[str, Outage], ...]
    power_windows: tuple[PowerWindow, ...]


@dataclass(frozen=True)
class VehicleSpec:
    battery_kwh: float
    kwh_per_km_empty: float
    kwh_per_km_loaded: float
    rated_payload_tonnes: float
    reserve_kwh: float


@dataclass(frozen=True)
class PlannedLeg:
    seq: int
    station_id: str
    arrive_at: datetime
    slot_start: datetime
    slot_end: datetime
    power_kw: float
    charge_kwh: float
    arrive_energy_kwh: float
    leave_energy_kwh: float
    arrive_margin_kwh: float
    arrive_margin_km: float
    power_margin_kw: float
    draws: tuple[tuple[datetime, float], ...] = ()
    charger_id: str = ""


@dataclass(frozen=True)
class PlanOutcome:
    feasible: bool
    reasons: tuple[str, ...]
    legs: tuple[PlannedLeg, ...]
    anchor_km: float
    anchor_at: datetime
    anchor_energy_kwh: float
    consumption_kwh_per_km: float
    destination_arrive_at: datetime | None
    destination_energy_kwh: float | None
    dest_margin_kwh: float | None
    dest_margin_km: float | None
    time_margin_minutes: float | None
    total_charge_kwh: float
    total_wait_minutes: float

    def to_serializable(self) -> dict:
        def iso(moment: datetime | None) -> str | None:
            return None if moment is None else moment.isoformat().replace("+00:00", "Z")

        return {
            "feasible": self.feasible,
            "reasons": list(self.reasons),
            "anchor_km": self.anchor_km,
            "anchor_at": iso(self.anchor_at),
            "anchor_energy_kwh": round(self.anchor_energy_kwh, 3),
            "consumption_kwh_per_km": round(self.consumption_kwh_per_km, 4),
            "destination_arrive_at": iso(self.destination_arrive_at),
            "destination_energy_kwh": (None if self.destination_energy_kwh is None
                                       else round(self.destination_energy_kwh, 3)),
            "dest_margin_kwh": None if self.dest_margin_kwh is None else round(self.dest_margin_kwh, 3),
            "dest_margin_km": None if self.dest_margin_km is None else round(self.dest_margin_km, 2),
            "time_margin_minutes": None if self.time_margin_minutes is None
                                   else round(self.time_margin_minutes, 1),
            "total_charge_kwh": round(self.total_charge_kwh, 3),
            "total_wait_minutes": round(self.total_wait_minutes, 1),
            "legs": [
                {
                    "seq": leg.seq,
                    "station_id": leg.station_id,
                    "arrive_at": iso(leg.arrive_at),
                    "slot_start": iso(leg.slot_start),
                    "slot_end": iso(leg.slot_end),
                    "charger_id": leg.charger_id,
                    "power_kw": round(leg.power_kw, 2),
                    "charge_kwh": round(leg.charge_kwh, 3),
                    "arrive_energy_kwh": round(leg.arrive_energy_kwh, 3),
                    "leave_energy_kwh": round(leg.leave_energy_kwh, 3),
                    "arrive_margin_kwh": round(leg.arrive_margin_kwh, 3),
                    "arrive_margin_km": round(leg.arrive_margin_km, 2),
                    "power_margin_kw": round(leg.power_margin_kw, 2),
                    "draws": [
                        [iso(moment), round(power, 2)] for moment, power in leg.draws
                    ],
                }
                for leg in self.legs
            ],
        }


@dataclass(frozen=True)
class ReachPoint:
    station_id: str
    position_km: float
    path_km: float
    arrive_at: datetime
    arrive_energy_kwh: float
    margin_kwh: float
    margin_km: float
    safely_reachable: bool


class OccupancyView:
    """读取某站某时隙已经被锁定的功率与物理桩占用。"""

    def __init__(self, slots: dict[tuple[str, datetime], dict] | None = None) -> None:
        self._slots = slots or {}

    def slot(self, station_id: str, moment: datetime) -> dict:
        return self._slots.get((station_id, moment), {"used_kw": 0.0, "busy": {}})


class NetworkSnapshot:
    """一次规划使用的完整只读网络快照。"""

    def __init__(self, *, stations: list[StationSpec], segments: list[RoadSegment],
                 avg_speed_kmh: float, occupancy: OccupancyView | None = None) -> None:
        self.stations = sorted(stations, key=lambda item: item.position_km)
        self.segments = sorted(segments, key=lambda item: item.from_km)
        self.avg_speed_kmh = avg_speed_kmh
        self.occupancy = occupancy or OccupancyView()

    def consumption(self, vehicle: VehicleSpec, load_tonnes: float) -> float:
        ratio = min(max(load_tonnes, 0.0) / vehicle.rated_payload_tonnes, 1.5)
        span = vehicle.kwh_per_km_loaded - vehicle.kwh_per_km_empty
        return vehicle.kwh_per_km_empty + span * min(ratio, 1.0)

    def path_distance(self, from_km: float, to_km: float) -> float:
        """考虑封闭段绕行后的实际行驶里程（方向为桩号增大方向）。"""

        if to_km < from_km:
            raise ValueError("干线方向上 to_km 不能小于 from_km")
        distance = to_km - from_km
        for segment in self.segments:
            if segment.to_km <= from_km or segment.from_km >= to_km:
                continue
            if not segment.open:
                distance += segment.detour_extra_km
        return distance

    def travel_minutes(self, distance_km: float) -> float:
        return distance_km / self.avg_speed_kmh * 60.0

    def station_open(self, station: StationSpec, moment: datetime) -> bool:
        if not station.active:
            return False
        return not any(outage.covers(moment) for outage in station.station_outages)

    def charger_open(self, station: StationSpec, charger_id: str, moment: datetime) -> bool:
        for cid, outage in station.charger_outages:
            if cid == charger_id and outage.covers(moment):
                return False
        return True

    def window_cap(self, station: StationSpec, moment: datetime) -> float | None:
        caps = [window.cap_kw for window in station.power_windows if window.covers(moment)]
        return min(caps) if caps else None

    def slot_power(self, station: StationSpec, charger: ChargerSpec, moment: datetime) -> float:
        """单个物理桩在某时隙可提供的功率，已扣除站点限额与既有占用。"""

        if not self.station_open(station, moment) or not charger.active:
            return 0.0
        if not self.charger_open(station, charger.charger_id, moment):
            return 0.0
        occupied = self.occupancy.slot(station.station_id, moment)
        busy = occupied["busy"]
        if charger.charger_id in busy:
            return 0.0
        cap = self.window_cap(station, moment)
        if cap is None:
            return charger.rated_power_kw
        used_other = sum(draw for cid, draw in busy.items() if cid != charger.charger_id)
        return max(0.0, min(charger.rated_power_kw, cap - used_other))


def _infeasible(reasons, *, anchor_km, anchor_at, anchor_energy, consumption) -> PlanOutcome:
    return PlanOutcome(False, tuple(reasons), (), anchor_km, anchor_at, anchor_energy, consumption,
                       None, None, None, None, None, 0.0, 0.0)


def plan_trip(*, vehicle: VehicleSpec, load_tonnes: float, network: NetworkSnapshot,
              anchor_km: float, anchor_at: datetime, anchor_energy_kwh: float,
              destination_km: float, deadline: datetime) -> PlanOutcome:
    """基于能量锚点生成补能计划。

    采用"最远可达 + 一次充满（或足够直达终点）"贪心：每次选择剩余续航内最远、
    且能在任务时限前完成补能的站点；封闭段按绕行里程计入能耗与时间。
    """

    consumption = network.consumption(vehicle, load_tonnes)
    energy = anchor_energy_kwh
    pos = anchor_km
    clock = anchor_at
    legs: list[PlannedLeg] = []
    total_wait = 0.0
    reasons: list[str] = []
    seq = 0

    if not (0.0 <= energy <= vehicle.battery_kwh + 1e-6):
        return _infeasible(("锚点电量超出电池范围",), anchor_km=anchor_km, anchor_at=anchor_at,
                           anchor_energy=anchor_energy_kwh, consumption=consumption)
    if destination_km < anchor_km:
        return _infeasible(("终点桩号不能位于锚点之前",), anchor_km=anchor_km, anchor_at=anchor_at,
                           anchor_energy=anchor_energy_kwh, consumption=consumption)

    guard = 0
    while True:
        guard += 1
        if guard > 64:
            return _infeasible(tuple(reasons + ["规划迭代超出上限"]), anchor_km=anchor_km, anchor_at=anchor_at,
                               anchor_energy=anchor_energy_kwh, consumption=consumption)
        dist_dest = network.path_distance(pos, destination_km)
        need_dest = dist_dest * consumption
        if energy + 1e-6 >= need_dest + vehicle.reserve_kwh:
            dest_arrival = clock + timedelta(minutes=network.travel_minutes(dist_dest))
            dest_energy = energy - need_dest
            margin_kwh = dest_energy - vehicle.reserve_kwh
            time_margin = (deadline - dest_arrival).total_seconds() / 60.0
            if time_margin < 0:
                reasons.append("即使不再补能也无法在任务时限前到达")
                return _infeasible(tuple(reasons), anchor_km=anchor_km, anchor_at=anchor_at,
                                   anchor_energy=anchor_energy_kwh, consumption=consumption)
            return PlanOutcome(True, tuple(reasons), tuple(legs), anchor_km, anchor_at,
                               anchor_energy_kwh, consumption, dest_arrival, dest_energy,
                               margin_kwh, margin_kwh / consumption, time_margin,
                               sum(leg.charge_kwh for leg in legs), total_wait)

        reach_km = max(0.0, (energy - vehicle.reserve_kwh) / consumption)
        ahead = [s for s in network.stations
                 if pos - 1e-9 <= s.position_km <= destination_km + 1e-9
                 and network.path_distance(pos, s.position_km) <= reach_km + 1e-9]
        serviceable: list[tuple[StationSpec, dict]] = []
        for station in reversed(ahead):
            dist = network.path_distance(pos, station.position_km)
            if dist <= 1e-9 and abs(station.position_km - pos) < 1e-9:
                arrive = clock
                arrive_energy = energy
            else:
                arrive = clock + timedelta(minutes=network.travel_minutes(dist))
                arrive_energy = energy - dist * consumption
            if arrive_energy + 1e-6 < vehicle.reserve_kwh:
                continue
            bout = _simulate_bout(vehicle=vehicle, network=network, station=station,
                                  arrive=arrive, arrive_energy=arrive_energy,
                                  consumption=consumption, destination_km=destination_km,
                                  pos=pos, deadline=deadline)
            if bout is not None:
                serviceable.append((station, bout))
            if serviceable:
                break
        if not serviceable:
            nearest = ahead[-1] if ahead else None
            if nearest is None:
                reasons.append("剩余续航内没有可用充电站")
            else:
                reasons.append(f"剩余续航内的站点 {nearest.station_id} 无法在时限前完成补能")
            return _infeasible(tuple(reasons), anchor_km=anchor_km, anchor_at=anchor_at,
                               anchor_energy=anchor_energy_kwh, consumption=consumption)

        station, bout = serviceable[0]
        seq += 1
        draws: list[tuple[datetime, float]] = []
        min_power = float("inf")
        for piece in bout["pieces"]:
            for slot, power in _piece_slot_draws(piece):
                draws.append((slot, power))
                min_power = min(min_power, power)
        total_kwh = bout["pieces"][-1]["leave_energy"] - bout["arrive_energy"]
        legs.append(PlannedLeg(
            seq=seq,
            station_id=station.station_id,
            arrive_at=bout["arrive"],
            slot_start=bout["pieces"][0]["start"],
            slot_end=bout["pieces"][-1]["end"],
            power_kw=_average_draw(draws),
            charge_kwh=round(total_kwh, 6),
            arrive_energy_kwh=bout["arrive_energy"],
            leave_energy_kwh=bout["pieces"][-1]["leave_energy"],
            arrive_margin_kwh=bout["arrive_energy"] - vehicle.reserve_kwh,
            arrive_margin_km=(bout["arrive_energy"] - vehicle.reserve_kwh) / consumption,
            power_margin_kw=min(p["power_margin"] for p in bout["pieces"]),
            draws=tuple(draws),
            charger_id=bout["pieces"][0]["charger_id"],
        ))
        total_wait += bout["wait_minutes"]
        energy = bout["leave_energy"]
        pos = station.position_km
        clock = bout["end"]


def _piece_slot_draws(piece: dict):
    """把一段常量功率充电展开为 (时隙起点, 该时隙取功 kW)。

    占用按常量功率记账，即使最后一个时隙只充了一部分，也按该功率保留整时隙容量，
    保证功率限额校验偏保守、绝不超载。
    """

    slot = floor_slot(piece["start"])
    while slot < piece["end"]:
        yield slot, piece["power"]
        slot += timedelta(minutes=SLOT_MINUTES)


def _average_draw(draws: list[tuple[datetime, float]]) -> float:
    powers = [power for _, power in draws]
    return sum(powers) / len(powers)


def _simulate_bout(*, vehicle: VehicleSpec, network: NetworkSnapshot, station: StationSpec,
                   arrive: datetime, arrive_energy: float, consumption: float,
                   destination_km: float, pos: float, deadline: datetime):
    """在单个站点尝试充电，返回按功率档位切分的常量功率充电段。"""

    target = min(vehicle.battery_kwh,
                 vehicle.reserve_kwh + network.path_distance(station.position_km, destination_km) * consumption)
    desired = target - arrive_energy
    if desired <= 1e-6:
        return None
    first_slot = ceil_slot(arrive)
    horizon_slots = int(((deadline - first_slot).total_seconds() / 60.0) // SLOT_MINUTES) + 2
    if horizon_slots <= 0:
        return None

    best: dict | None = None
    for charger in station.chargers:
        pieces = _build_pieces(network, station, charger, first_slot, horizon_slots,
                               arrive_energy, target, vehicle.battery_kwh)
        if pieces is None:
            continue
        finish = pieces[-1]["end"]
        if finish > deadline:
            continue
        if best is None or finish < best["finish"]:
            best = {"charger": charger, "pieces": pieces, "finish": finish}
    if best is None:
        return None

    pieces = best["pieces"]
    return {
        "arrive": arrive,
        "arrive_energy": arrive_energy,
        "wait_minutes": (pieces[0]["start"] - arrive).total_seconds() / 60.0,
        "pieces": pieces,
        "end": best["finish"],
        "leave_energy": pieces[-1]["leave_energy"],
    }


def _build_pieces(network: NetworkSnapshot, station: StationSpec, charger: ChargerSpec,
                  first_slot: datetime, horizon_slots: int, arrive_energy: float,
                  target: float, battery_kwh: float) -> list[dict] | None:
    """沿时隙推进，按可用功率档位把一次充电切成常量功率段。"""

    energy = arrive_energy
    pieces: list[dict] = []
    piece_start: datetime | None = None
    piece_power = 0.0
    piece_kwh = 0.0
    piece_margin = float("inf")
    slot = first_slot

    def close_piece(end: datetime) -> None:
        nonlocal piece_start, piece_power, piece_kwh, piece_margin
        pieces.append({"start": piece_start, "end": end, "power": piece_power,
                       "kwh": piece_kwh, "power_margin": max(0.0, piece_margin)})
        piece_start = None
        piece_power = 0.0
        piece_kwh = 0.0
        piece_margin = float("inf")

    fleet = sum(c.rated_power_kw for c in station.chargers if c.active)
    scanned = 0
    while energy + 1e-6 < target and scanned < horizon_slots:
        power = network.slot_power(station, charger, slot)
        cap = network.window_cap(station, slot)
        power_margin = (cap - power) if cap is not None else max(0.0, fleet - power)
        scanned += 1
        if power <= 1e-6:
            if piece_start is not None:
                close_piece(slot)
            slot += timedelta(minutes=SLOT_MINUTES)
            continue
        room = max(0.0, min(target, battery_kwh) - energy)
        delivered = min(power * SLOT_HOURS, room)
        if piece_start is None:
            piece_start = slot
            piece_power = power
        elif abs(power - piece_power) > 1e-6:
            close_piece(slot)
            piece_start = slot
            piece_power = power
        piece_margin = min(piece_margin, power_margin)
        piece_kwh += delivered
        energy += delivered
        if delivered + 1e-6 >= power * SLOT_HOURS:
            slot += timedelta(minutes=SLOT_MINUTES)
        else:
            # 在当前时隙内部充到目标值，按实际功率折算结束时刻
            finish = slot + timedelta(minutes=delivered / power * 60.0)
            close_piece(finish)

    if energy + 1e-6 < target:
        return None
    if piece_start is not None:
        close_piece(slot)
    # 合并恰好相邻且同功率的段
    merged: list[dict] = []
    for piece in pieces:
        if merged and merged[-1]["end"] == piece["start"] and abs(merged[-1]["power"] - piece["power"]) < 1e-6:
            previous = merged.pop()
            piece = {**piece, "start": previous["start"], "kwh": previous["kwh"] + piece["kwh"],
                     "power_margin": min(previous["power_margin"], piece["power_margin"])}
        merged.append(piece)
    cumulative = arrive_energy
    for piece in merged:
        piece["charger_id"] = charger.charger_id
        piece["leave_energy"] = min(target, cumulative + piece["kwh"])
        cumulative = piece["leave_energy"]
    return merged


def evaluate_reach(*, vehicle: VehicleSpec, load_tonnes: float, network: NetworkSnapshot,
                   anchor_km: float, anchor_at: datetime, anchor_energy_kwh: float,
                   destination_km: float) -> dict:
    """运营接口：从当前能量锚点判断各站点与终点是否仍可安全抵达。"""

    consumption = network.consumption(vehicle, load_tonnes)
    points: list[ReachPoint] = []
    for station in network.stations:
        if station.position_km < anchor_km - 1e-9 or station.position_km > destination_km + 1e-9:
            continue
        dist = network.path_distance(anchor_km, station.position_km)
        arrive_energy = anchor_energy_kwh - dist * consumption
        arrive_at = anchor_at + timedelta(minutes=network.travel_minutes(dist))
        margin = arrive_energy - vehicle.reserve_kwh
        points.append(ReachPoint(station.station_id, station.position_km, dist, arrive_at,
                                 arrive_energy, margin, margin / consumption, margin >= -1e-6))
    dist_dest = network.path_distance(anchor_km, destination_km)
    dest_energy = anchor_energy_kwh - dist_dest * consumption
    dest_margin = dest_energy - vehicle.reserve_kwh
    return {
        "consumption_kwh_per_km": round(consumption, 4),
        "remaining_safe_km": round(max(0.0, dest_margin) / consumption, 2),
        "stations": [
            {
                "station_id": point.station_id,
                "position_km": point.position_km,
                "path_km": round(point.path_km, 2),
                "arrive_at": point.arrive_at.isoformat().replace("+00:00", "Z"),
                "arrive_energy_kwh": round(point.arrive_energy_kwh, 3),
                "margin_kwh": round(point.margin_kwh, 3),
                "margin_km": round(point.margin_km, 2),
                "safely_reachable": point.safely_reachable,
            }
            for point in points
        ],
        "destination": {
            "position_km": destination_km,
            "path_km": round(dist_dest, 2),
            "arrive_at": (anchor_at + timedelta(minutes=network.travel_minutes(dist_dest)))
                .isoformat().replace("+00:00", "Z"),
            "arrive_energy_kwh": round(dest_energy, 3),
            "margin_kwh": round(dest_margin, 3),
            "margin_km": round(dest_margin / consumption, 2),
            "safely_reachable": dest_margin >= -1e-6,
        },
    }
