"""干线充电保障的核心运行服务。

关键边界：
- 所有容量变化都在 ``BEGIN IMMEDIATE`` 事务内完成，多站确认要么全部成功要么全部回滚；
- 容量扣减以 ``reservation_slot_ledger`` 为唯一台账，幂等回执保证重试不重复扣减；
- 重规划只以 ``trip_state_facts`` 中已经发生的事实为起点，不再相信旧计划的假设；
- 抢险优先权只能挤掉尚未到站（``reserved``）的普通预约，已开始会话受到保护。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .charging_admin import ChargingAdminService
from .charging_models import (PlanStopView, PlanView, ReplanView, ReservationStopView,
                              ReservationView)
from .charging_planning import (SLOT_MINUTES, RoutePoint, plan_with_fallback,
                                shortest_route, slot_floor, slot_range)
from .errors import (CapacityError, ConflictError, NotFoundError, PlanningError,
                     StaleVersionError, ValidationError)

DEFAULT_PLAN_TTL_MINUTES = 10


def parse_time(value: str | datetime) -> datetime:
    """把 ISO 时间统一为带时区的 UTC datetime。"""

    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ChargingService(ChargingAdminService):
    """面向调度席位、司机端与运营接口的充电保障服务。"""

    def __init__(self, database, clock=None, plan_ttl_minutes: int = DEFAULT_PLAN_TTL_MINUTES) -> None:
        super().__init__(database, clock)
        self.plan_ttl_minutes = plan_ttl_minutes

    # ================================================================ 工具

    def _trip(self, connection, trip_id: str) -> Any:
        trip = connection.execute("SELECT * FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
        if trip is None:
            raise NotFoundError("运输任务不存在")
        return trip

    def _active_reservation(self, connection, trip_id: str) -> Any | None:
        return connection.execute(
            "SELECT * FROM reservations WHERE trip_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (trip_id,),
        ).fetchone()

    def _slot_caps(self, connection, station_id: str, slot: datetime) -> dict[str, float]:
        """计算某时隙的桩数上限与功率上限（分时功率限额生效）。"""

        equipment = self.get_station_equipment(station_id)
        operational = [c for c in equipment.chargers if c["status"] == "operational"]
        charger_power = sum(c["max_power_kw"] for c in operational)
        minute = slot.hour * 60 + slot.minute
        day = slot.date().isoformat()
        rows = connection.execute(
            "SELECT start_minute,end_minute,max_power_kw,effective_date FROM power_schedules "
            "WHERE station_id=? AND (effective_date IS NULL OR effective_date=?)",
            (station_id, day)).fetchall()
        dated = [r["max_power_kw"] for r in rows if r["effective_date"] == day
                 and r["start_minute"] < minute + SLOT_MINUTES and r["end_minute"] > minute]
        if dated:
            scheduled = min(dated)
        else:
            generic = [r["max_power_kw"] for r in rows if r["effective_date"] is None
                       and r["start_minute"] < minute + SLOT_MINUTES and r["end_minute"] > minute]
            scheduled = min(generic) if generic else charger_power
        return {"cap_chargers": float(len(operational)),
                "cap_power": min(charger_power, scheduled),
                "queue_timeout_minutes": equipment.queue_timeout_minutes}

    def _slot_usage(self, connection, station_id: str, slot: datetime,
                    exclude_reservation: str | None = None) -> dict[str, float]:
        """汇总台账上某时隙的实际占用（桩数与功率）。"""

        query = ("SELECT COUNT(*) AS sessions, COALESCE(SUM(power_kw),0) AS used_power "
                 "FROM reservation_slot_ledger WHERE station_id=? AND slot_start=? AND active=1")
        parameters: list[Any] = [station_id, format_time(slot)]
        if exclude_reservation is not None:
            query += " AND reservation_id != ?"
            parameters.append(exclude_reservation)
        row = connection.execute(query, parameters).fetchone()
        return {"used_chargers": float(row["sessions"]), "used_power": float(row["used_power"])}

    def _has_capacity(self, connection, station_id: str, slot: datetime, power_kw: float,
                      exclude_reservation: str | None = None) -> bool:
        view = self._capacity_view(connection, station_id, slot, exclude_reservation)
        return (view["available_chargers"] >= 1 - 1e-6
                and view["available_power"] + 1e-6 >= power_kw)

    def _capacity_view(self, connection, station_id: str, slot: datetime,
                       exclude_reservation: str | None = None) -> dict[str, float]:
        caps = self._slot_caps(connection, station_id, slot)
        usage = self._slot_usage(connection, station_id, slot, exclude_reservation)
        return {**caps, **usage,
                "available_chargers": max(0.0, caps["cap_chargers"] - usage["used_chargers"]),
                "available_power": max(0.0, caps["cap_power"] - usage["used_power"])}

    # ================================================================ 行程

    def register_trip(self, *, request_id: str, actor_id: str, trip_id: str, vehicle_id: str,
                      battery_kwh: float, current_energy_kwh: float, depart_at: str,
                      max_charge_kw: float, rate_kwh_per_km: float, origin_node: str,
                      destination_node: str, arrive_by: str, reserve_km: float = 50.0,
                      load_tons: float = 0.0, priority: str = "normal",
                      road_version: int | None = None) -> Any:
        payload = {"trip_id": trip_id, "vehicle_id": vehicle_id, "battery_kwh": battery_kwh,
                   "current_energy_kwh": current_energy_kwh, "depart_at": depart_at,
                   "max_charge_kw": max_charge_kw, "rate_kwh_per_km": rate_kwh_per_km,
                   "origin_node": origin_node, "destination_node": destination_node,
                   "arrive_by": arrive_by, "reserve_km": reserve_km, "load_tons": load_tons,
                   "priority": priority, "road_version": road_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            trip_id = self._identifier(trip_id, "trip_id")
            vehicle_id = self._identifier(vehicle_id, "vehicle_id")
            battery = float(battery_kwh)
            energy = float(current_energy_kwh)
            max_kw = float(max_charge_kw)
            rate = float(rate_kwh_per_km)
            reserve = float(reserve_km)
            load = float(load_tons)
            if battery <= 0 or max_kw <= 0 or rate <= 0:
                raise ValidationError("电池容量、最大充电功率和百公里电耗系数必须为正")
            if not 0 <= energy <= battery:
                raise ValidationError("当前电量必须在 0 与电池容量之间")
            if reserve < 0 or load < 0:
                raise ValidationError("安全续航余量与载重不能为负")
            if priority not in {"normal", "rescue"}:
                raise ValidationError("priority 必须是 normal 或 rescue")
            depart = parse_time(depart_at)
            deadline = parse_time(arrive_by)
            if deadline <= depart:
                raise ValidationError("任务时限必须晚于发车时间")
            network = self.get_road_network(road_version)
            if origin_node not in network.nodes or destination_node not in network.nodes:
                raise ValidationError("起终点节点必须存在于道路网络")
            version = network.version

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO trips(trip_id,vehicle_id,priority,battery_kwh,initial_energy_kwh,"
                        "depart_at,max_charge_kw,load_tons,rate_kwh_per_km,origin_node,destination_node,"
                        "arrive_by,reserve_km,road_version,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (trip_id, vehicle_id, priority, battery, energy, format_time(depart),
                         max_kw, load, rate, origin_node, destination_node, format_time(deadline),
                         reserve, version, "planned", self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("运输任务编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="trip.registered",
                             resource_type="trip", resource_id=trip_id,
                             detail={"vehicle_id": vehicle_id, "priority": priority,
                                     "road_version": version, "origin_node": origin_node,
                                     "destination_node": destination_node},
                             occurred_at=self._now())
                return "trip", trip_id, {"trip_id": trip_id, "road_version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_trip", payload=payload, create=create)

    # ================================================================ 计划

    def _route_context(self, connection, trip: Any, network: Any,
                       start_node: str | None = None) -> tuple[list[RoutePoint], dict[str, Any], dict[str, str]]:
        origin = start_node or trip["origin_node"]
        route = shortest_route(network, origin, trip["destination_node"])
        equipment: dict[str, Any] = {}
        names: dict[str, str] = {}
        for point in route:
            if point.station_id and point.station_id not in equipment:
                equipment[point.station_id] = self.get_station_equipment(point.station_id)
                names[point.station_id] = equipment[point.station_id].name
        return route, equipment, names

    def _proposal_capacity_fn(self, connection, exclude_reservation: str | None,
                              rescue: bool = False):
        """规划阶段的容量口径。

        抢险规划把"尚未到站的普通预约"视为可抢占的软占用：只统计其他抢险预约
        与已经产生事实（已到站/充电中）的会话为硬占用。
        """

        def capacity(station_id: str, slot: datetime) -> dict[str, float]:
            caps = self._slot_caps(connection, station_id, slot)
            if rescue:
                query = (
                    "SELECT COUNT(*) AS sessions, COALESCE(SUM(l.power_kw),0) AS used_power "
                    "FROM reservation_slot_ledger l JOIN reservations r ON r.reservation_id=l.reservation_id "
                    "JOIN trips t ON t.trip_id=l.trip_id "
                    "WHERE l.active=1 AND r.status='active' AND l.station_id=? AND l.slot_start=? "
                    "AND (t.priority='rescue' OR EXISTS (SELECT 1 FROM reservation_stops rs "
                    "     WHERE rs.reservation_id=l.reservation_id AND rs.is_fact=1))")
                parameters: list[Any] = [station_id, format_time(slot)]
            else:
                query = ("SELECT COUNT(*) AS sessions, COALESCE(SUM(power_kw),0) AS used_power "
                         "FROM reservation_slot_ledger WHERE active=1 AND station_id=? AND slot_start=?")
                parameters = [station_id, format_time(slot)]
            if exclude_reservation is not None:
                query += " AND l.reservation_id != ?" if rescue else " AND reservation_id != ?"
                parameters.append(exclude_reservation)
            row = connection.execute(query, parameters).fetchone()
            return {**caps, "used_chargers": float(row["sessions"]),
                    "used_power": float(row["used_power"])}
        return capacity

    def generate_plan(self, *, actor_id: str, trip_id: str,
                      current_energy_kwh: float | None = None,
                      start_node: str | None = None, start_at: str | None = None,
                      road_version: int | None = None) -> PlanView:
        """基于最新道路版本、设备与分时功率生成一版带有效期的补能计划。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            trip = self._trip(connection, trip_id)
            now = self.clock.now()
            network = self.get_road_network(road_version)
            route, equipment, names = self._route_context(connection, trip, network, start_node)
            energy = float(current_energy_kwh) if current_energy_kwh is not None else trip["initial_energy_kwh"]
            if not 0 <= energy <= trip["battery_kwh"]:
                raise ValidationError("剩余电量必须在 0 与电池容量之间")
            if start_node and start_node not in network.nodes:
                raise ValidationError("起始节点不在当前道路版本中")
            depart_at = parse_time(start_at) if start_at else parse_time(trip["depart_at"])
            result = plan_with_fallback(
                route=route, station_equipment=equipment,
                initial_energy_kwh=energy, battery_kwh=trip["battery_kwh"],
                max_charge_kw=trip["max_charge_kw"], rate_kwh_per_km=trip["rate_kwh_per_km"],
                reserve_km=trip["reserve_km"], depart_at=depart_at,
                arrive_by=parse_time(trip["arrive_by"]),
                capacity_fn=self._proposal_capacity_fn(
                    connection, None, rescue=trip["priority"] == "rescue"),
                station_names=names)
            version_seq = (connection.execute(
                "SELECT COALESCE(MAX(version_seq),0)+1 AS seq FROM plans WHERE trip_id=?",
                (trip_id,)).fetchone())["seq"]
            connection.execute("UPDATE plans SET status='superseded' WHERE trip_id=? AND status='proposed'",
                               (trip_id,))
            plan_id = uuid.uuid4().hex
            station_ids = [s.station_id for s in result.stops]
            station_digests = {sid: equipment[sid].digest for sid in station_ids}
            valid_until = now + timedelta(minutes=self.plan_ttl_minutes)
            metrics = {"destination_arrive_at": format_time(result.destination_arrive_at)
                       if result.destination_arrive_at else None,
                       "destination_margin_km": round(result.destination_margin_km, 3),
                       "total_charge_kwh": round(result.total_charge_kwh, 3),
                       "total_wait_minutes": round(result.total_wait_minutes, 2),
                       "excluded_stations": sorted(
                           set(p.station_id for p in route if p.station_id) - set(station_ids))
                       if route else []}
            summary = {"infeasible_reason": result.reason, "metrics": metrics,
                       "stations": station_ids, "station_digests": station_digests,
                       "route_node_ids": [p.node_id for p in route],
                       "start_node": start_node or trip["origin_node"],
                       "start_energy_kwh": energy,
                       "start_at": format_time(depart_at)}
            connection.execute(
                "INSERT INTO plans(plan_id,trip_id,version_seq,status,feasible,road_version,"
                "equipment_digest,valid_from,valid_until,summary_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, trip_id, version_seq, "proposed", 1 if result.feasible else 0,
                 network.version, digest(station_digests), format_time(now), format_time(valid_until),
                 canonical_json(summary), format_time(now)))
            for stop in result.stops:
                connection.execute(
                    "INSERT INTO plan_stops(stop_id,plan_id,seq,station_id,arrive_at,depart_at,"
                    "slot_start,slot_end,charge_kwh,planned_power_kw,arrive_range_km,reserve_margin_km) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, plan_id, stop.seq, stop.station_id,
                     format_time(stop.arrive_at), format_time(stop.depart_at),
                     format_time(stop.charge_start), format_time(stop.depart_at),
                     stop.charge_kwh, stop.planned_power_kw, stop.arrive_range_km,
                     stop.reserve_margin_km))
            append_event(connection, actor_id=actor_id,
                         action="plan.generated" if result.feasible else "plan.infeasible",
                         resource_type="plan", resource_id=plan_id,
                         detail={"trip_id": trip_id, "version_seq": version_seq,
                                 "road_version": network.version, "feasible": result.feasible,
                                 "reason": result.reason, "stations": station_ids},
                         occurred_at=self._now())
            return self._load_plan(connection, plan_id)

    def _load_plan(self, connection, plan_id: str) -> PlanView:
        row = connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("补能计划不存在")
        summary = json.loads(row["summary_json"])
        stops = []
        for s in connection.execute("SELECT * FROM plan_stops WHERE plan_id=? ORDER BY seq", (plan_id,)):
            station = connection.execute("SELECT name FROM charging_stations WHERE station_id=?",
                                         (s["station_id"],)).fetchone()
            stops.append(PlanStopView(
                seq=s["seq"], station_id=s["station_id"],
                station_name=station["name"] if station else s["station_id"],
                arrive_at=s["arrive_at"], depart_at=s["depart_at"],
                slot_start=s["slot_start"], slot_end=s["slot_end"],
                charge_kwh=s["charge_kwh"], planned_power_kw=s["planned_power_kw"],
                arrive_range_km=round(s["arrive_range_km"], 3),
                reserve_margin_km=round(s["reserve_margin_km"], 3)))
        return PlanView(plan_id=plan_id, trip_id=row["trip_id"], version_seq=row["version_seq"],
                        status=row["status"], feasible=bool(row["feasible"]),
                        road_version=row["road_version"], valid_from=row["valid_from"],
                        valid_until=row["valid_until"], created_at=row["created_at"],
                        infeasible_reason=summary.get("infeasible_reason"),
                        stops=tuple(stops), metrics=summary.get("metrics", {}))

    def get_plan(self, plan_id: str) -> PlanView:
        return self._load_plan(self.database.connection, plan_id)

    # ================================================================ 确认

    def _check_plan_freshness(self, connection, plan: Any, now: datetime) -> Any:
        """确认前校验计划有效期、道路版本与各站点设备/功率摘要。"""

        if plan["status"] != "proposed":
            raise ConflictError("该计划已确认或已被取代，不能重复锁定")
        if not plan["feasible"]:
            raise PlanningError("不可行计划不能确认")
        if now > parse_time(plan["valid_until"]):
            raise StaleVersionError("计划已超过有效期，请重新生成")
        latest = self.get_road_network(None)
        if latest.version != plan["road_version"]:
            raise StaleVersionError(
                f"道路已发布新版本 {latest.version}，计划仍基于 {plan['road_version']}，请重新规划")
        summary = json.loads(plan["summary_json"])
        for station_id, recorded_digest in summary.get("station_digests", {}).items():
            current = self.get_station_equipment(station_id)
            if current.digest != recorded_digest:
                raise StaleVersionError(f"站点 {station_id} 设备或分时功率已变化，请重新规划")
        return summary

    def _preempt_normal(self, connection, station_id: str, slot: datetime,
                        required_power: float, rescued_trip_id: str) -> int:
        """为抢险车辆挤掉尚未开始的普通预约，返回释放出的会话数。"""

        candidates = connection.execute(
            "SELECT l.reservation_id, MAX(r.created_at) AS booked_at "
            "FROM reservation_slot_ledger l JOIN reservations r ON r.reservation_id=l.reservation_id "
            "JOIN trips t ON t.trip_id=l.trip_id "
            "WHERE l.active=1 AND r.status='active' AND t.priority='normal' "
            "AND l.station_id=? AND l.slot_start=? AND l.trip_id!=? "
            "AND l.res_stop_id IN (SELECT res_stop_id FROM reservation_stops "
            "                       WHERE status='reserved' AND is_fact=0) "
            "AND NOT EXISTS (SELECT 1 FROM reservation_stops rs WHERE rs.reservation_id=l.reservation_id "
            "                AND rs.is_fact=1) "
            "GROUP BY l.reservation_id ORDER BY booked_at DESC",
            (station_id, format_time(slot), rescued_trip_id),
        ).fetchall()
        displaced = 0
        for candidate in candidates:
            view = self._capacity_view(connection, station_id, slot)
            if view["available_chargers"] >= 1 - 1e-6 and \
                    view["available_power"] + 1e-6 >= required_power:
                return displaced
            if not self._displace_reservation(
                    connection, candidate["reservation_id"], reason="rescue_preemption",
                    detail={"station_id": station_id, "slot_start": format_time(slot),
                            "rescued_trip_id": rescued_trip_id}):
                continue
            displaced += 1
        return displaced

    def _displace_reservation(self, connection, reservation_id: str, *, reason: str,
                              detail: dict[str, Any]) -> bool:
        """整体取消一个尚未开始服务的普通预约，释放其全部时隙占用。

        已经产生任何事实（到站/充电中/已完成）的预约受保护，返回 False。
        """

        reservation = connection.execute("SELECT * FROM reservations WHERE reservation_id=?",
                                         (reservation_id,)).fetchone()
        if reservation is None:
            return False
        trip_id = reservation["trip_id"]
        protected = connection.execute(
            "SELECT COUNT(*) AS count FROM reservation_stops WHERE reservation_id=? AND is_fact=1",
            (reservation_id,)).fetchone()["count"]
        if protected:
            return False
        connection.execute(
            "UPDATE reservation_slot_ledger SET active=0 WHERE reservation_id=?", (reservation_id,))
        connection.execute(
            "UPDATE reservation_stops SET status='displaced' WHERE reservation_id=? AND status='reserved'",
            (reservation_id,))
        connection.execute("UPDATE reservations SET status='superseded' WHERE reservation_id=?",
                           (reservation_id,))
        connection.execute("UPDATE trips SET status='awaiting_replan' WHERE trip_id=?", (trip_id,))
        self._insert_fact(connection, trip_id=trip_id, kind="preempted",
                          occurred_at=self.clock.now(),
                          detail={"reason": reason, **detail})
        self._record_replan(connection, trip_id=trip_id, reason=reason,
                            detail={"reservation_id": reservation_id, **detail},
                            from_plan_id=reservation["plan_id"], to_plan_id=None)
        append_event(connection, actor_id="system", action="reservation.displaced",
                     resource_type="reservation", resource_id=reservation_id,
                     detail={"trip_id": trip_id, "reason": reason, **detail},
                     occurred_at=self._now())
        return True

    def confirm_plan(self, *, request_id: str, actor_id: str, plan_id: str):
        """司机确认：在单个事务内锁定计划涉及的全部站点时隙。"""

        payload = {"plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan = connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("补能计划不存在")
            trip = self._trip(connection, plan["trip_id"])
            now = self.clock.now()

            def create() -> tuple[str, str, dict[str, Any]]:
                self._check_plan_freshness(connection, plan, now)
                active = self._active_reservation(connection, trip["trip_id"])
                if active is not None:
                    raise ConflictError("该任务已有生效预约，请先基于最新事实改派")
                stops = connection.execute(
                    "SELECT * FROM plan_stops WHERE plan_id=? ORDER BY seq", (plan_id,)).fetchall()
                rescue = trip["priority"] == "rescue"
                # 第一遍：校验容量；抢险在不足时按规则抢占普通预约
                for stop in stops:
                    for slot in slot_range(parse_time(stop["slot_start"]),
                                           parse_time(stop["slot_end"])):
                        if self._has_capacity(connection, stop["station_id"], slot,
                                              stop["planned_power_kw"]):
                            continue
                        if not rescue:
                            raise CapacityError(
                                f"站点 {stop['station_id']} 时隙 {format_time(slot)} 容量不足")
                        self._preempt_normal(connection, stop["station_id"], slot,
                                             stop["planned_power_kw"], trip["trip_id"])
                        if not self._has_capacity(connection, stop["station_id"], slot,
                                                  stop["planned_power_kw"]):
                            raise CapacityError(
                                f"即使启用抢险优先权，站点 {stop['station_id']} "
                                f"时隙 {format_time(slot)} 仍无可用容量（已开始会话受保护）")
                reservation_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO reservations(reservation_id,trip_id,plan_id,status,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (reservation_id, trip["trip_id"], plan_id, "active", self._now()))
                for stop in stops:
                    res_stop_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO reservation_stops(res_stop_id,reservation_id,plan_stop_id,seq,"
                        "station_id,status,slot_start,slot_end,arrive_at,depart_at,charge_kwh,"
                        "planned_power_kw,delivered_kwh) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                        (res_stop_id, reservation_id, stop["stop_id"], stop["seq"],
                         stop["station_id"], "reserved", stop["slot_start"], stop["slot_end"],
                         stop["arrive_at"], stop["depart_at"], stop["charge_kwh"],
                         stop["planned_power_kw"]))
                    for slot in slot_range(parse_time(stop["slot_start"]),
                                           parse_time(stop["slot_end"])):
                        connection.execute(
                            "INSERT INTO reservation_slot_ledger(res_stop_id,reservation_id,trip_id,"
                            "station_id,slot_start,overlap_minutes,power_kw,active) "
                            "VALUES(?,?,?,?,?,?,?,1)",
                            (res_stop_id, reservation_id, trip["trip_id"], stop["station_id"],
                             format_time(slot), SLOT_MINUTES, stop["planned_power_kw"]))
                connection.execute("UPDATE plans SET status='confirmed' WHERE plan_id=?", (plan_id,))
                connection.execute("UPDATE trips SET status='confirmed' WHERE trip_id=?",
                                   (trip["trip_id"],))
                append_event(connection, actor_id=actor_id, action="plan.confirmed",
                             resource_type="reservation", resource_id=reservation_id,
                             detail={"trip_id": trip["trip_id"], "plan_id": plan_id,
                                     "stations": [s["station_id"] for s in stops],
                                     "priority": trip["priority"]},
                             occurred_at=self._now())
                return "reservation", reservation_id, {"reservation_id": reservation_id}

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="confirm_plan", payload=payload, create=create)
            return receipt, self._load_reservation(connection, receipt.resource_id)

    def _load_reservation(self, connection, reservation_id: str) -> ReservationView:
        row = connection.execute("SELECT * FROM reservations WHERE reservation_id=?",
                                 (reservation_id,)).fetchone()
        if row is None:
            raise NotFoundError("预约不存在")
        stops = []
        for s in connection.execute(
                "SELECT * FROM reservation_stops WHERE reservation_id=? ORDER BY seq",
                (reservation_id,)):
            stops.append(ReservationStopView(
                seq=s["seq"], station_id=s["station_id"], status=s["status"],
                slot_start=s["slot_start"], slot_end=s["slot_end"], arrive_at=s["arrive_at"],
                depart_at=s["depart_at"], charge_kwh=s["charge_kwh"],
                delivered_kwh=s["delivered_kwh"], is_fact=bool(s["is_fact"])))
        return ReservationView(reservation_id=reservation_id, trip_id=row["trip_id"],
                               plan_id=row["plan_id"], status=row["status"], stops=tuple(stops))

    def get_reservation(self, trip_id: str) -> ReservationView | None:
        row = self._active_reservation(self.database.connection, trip_id)
        if row is None:
            return None
        return self._load_reservation(self.database.connection, row["reservation_id"])

    # ================================================================ 事实

    def _insert_fact(self, connection, *, trip_id: str, kind: str,
                     node_id: str | None = None, station_id: str | None = None,
                     charger_id: str | None = None, energy_kwh: float | None = None,
                     delivered_kwh: float | None = None, occurred_at: datetime,
                     detail: dict[str, Any] | None = None) -> str:
        fact_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO trip_state_facts(fact_id,trip_id,kind,node_id,station_id,charger_id,"
            "energy_kwh,delivered_kwh,occurred_at,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (fact_id, trip_id, kind, node_id, station_id, charger_id, energy_kwh, delivered_kwh,
             format_time(occurred_at), canonical_json(detail or {})))
        return fact_id

    def _record_replan(self, connection, *, trip_id: str, reason: str, detail: dict[str, Any],
                       from_plan_id: str | None, to_plan_id: str | None) -> str:
        replan_id = uuid.uuid4().hex
        seq = (connection.execute("SELECT COALESCE(MAX(seq),0)+1 AS seq FROM replans WHERE trip_id=?",
                                  (trip_id,)).fetchone())["seq"]
        connection.execute(
            "INSERT INTO replans(replan_id,trip_id,seq,reason,from_plan_id,to_plan_id,detail_json,"
            "actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (replan_id, trip_id, seq, reason, from_plan_id, to_plan_id,
             canonical_json(detail), detail.get("actor_id", "system"), self._now()))
        return replan_id

    def _station_node(self, connection, station_id: str) -> str:
        row = connection.execute("SELECT node_id FROM charging_stations WHERE station_id=?",
                                 (station_id,)).fetchone()
        if row is None:
            raise NotFoundError("站点不存在")
        return row["node_id"]

    def report_event(self, *, request_id: str, actor_id: str, trip_id: str, event_type: str,
                     station_id: str | None = None, charger_id: str | None = None,
                     occurred_at: str | None = None, energy_kwh: float | None = None,
                     delivered_kwh: float | None = None, detail: dict[str, Any] | None = None):
        """登记一个已经发生的运行事实；同一 request_id 重试不会重复记账。"""

        payload = {"trip_id": trip_id, "event_type": event_type, "station_id": station_id,
                   "charger_id": charger_id, "energy_kwh": energy_kwh,
                   "delivered_kwh": delivered_kwh, "occurred_at": occurred_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            trip = self._trip(connection, trip_id)
            moment = parse_time(occurred_at) if occurred_at else self.clock.now()
            energy = float(energy_kwh) if energy_kwh is not None else None
            delivered = float(delivered_kwh) if delivered_kwh is not None else None
            if energy is not None and not 0 <= energy <= trip["battery_kwh"]:
                raise ValidationError("事实电量必须在 0 与电池容量之间")
            if event_type not in {"arrived", "charging", "charge_progress", "completed",
                                  "departed", "fault", "queue_timeout", "arrived_destination"}:
                raise ValidationError("event_type 不合法")

            def create() -> tuple[str, str, dict[str, Any]]:
                reservation = self._active_reservation(connection, trip_id)
                stop = None
                if station_id is not None:
                    stop = self._resolve_event_stop(connection, reservation, event_type, station_id)
                result_energy = self._apply_event(
                    connection, trip=trip, reservation=reservation, stop=stop,
                    event_type=event_type, station_id=station_id, charger_id=charger_id,
                    moment=moment, energy=energy, delivered=delivered, detail=detail or {},
                    actor_id=actor_id)
                fact_id = self._insert_fact(
                    connection, trip_id=trip_id, kind=event_type,
                    node_id=self._station_node(connection, station_id) if station_id else None,
                    station_id=station_id, charger_id=charger_id,
                    energy_kwh=result_energy, delivered_kwh=delivered,
                    occurred_at=moment, detail={"stop_seq": stop["seq"] if stop else None,
                                                **(detail or {})})
                append_event(connection, actor_id=actor_id, action=f"trip_event.{event_type}",
                             resource_type="trip", resource_id=trip_id,
                             detail={"station_id": station_id, "energy_kwh": result_energy,
                                     "delivered_kwh": delivered},
                             occurred_at=format_time(moment))
                return "trip_fact", fact_id, {"fact_id": fact_id, "energy_kwh": result_energy}

            receipt = self._idempotent(connection, request_id=request_id,
                                       action=f"report_event:{event_type}", payload=payload,
                                       create=create)
            return receipt

    def _resolve_event_stop(self, connection, reservation, event_type: str, station_id: str):
        if reservation is None:
            raise PlanningError("任务没有生效预约，请先确认计划")
        if event_type in {"arrived", "queue_timeout"}:
            stop = connection.execute(
                "SELECT * FROM reservation_stops WHERE reservation_id=? AND station_id=? "
                "AND status='reserved' ORDER BY seq LIMIT 1",
                (reservation["reservation_id"], station_id)).fetchone()
        elif event_type == "departed":
            stop = connection.execute(
                "SELECT * FROM reservation_stops WHERE reservation_id=? AND station_id=? "
                "AND status IN ('arrived','charging','completed') ORDER BY seq DESC LIMIT 1",
                (reservation["reservation_id"], station_id)).fetchone()
        else:
            stop = connection.execute(
                "SELECT * FROM reservation_stops WHERE reservation_id=? AND station_id=? "
                "AND status IN ('arrived','charging') ORDER BY seq DESC LIMIT 1",
                (reservation["reservation_id"], station_id)).fetchone()
        if stop is None:
            raise PlanningError(f"站点 {station_id} 没有匹配的进行中预约，若已改道请先重新规划")
        return stop

    def _apply_event(self, connection, *, trip, reservation, stop, event_type: str,
                     station_id: str | None, charger_id: str | None, moment: datetime,
                     energy: float | None, delivered: float | None, detail: dict[str, Any],
                     actor_id: str) -> float | None:
        """把事实写入预约状态并在需要时释放容量/置任务为待重规划。返回记账电量。"""

        trip_id = trip["trip_id"]
        if event_type == "arrived_destination":
            connection.execute("UPDATE trips SET status='completed' WHERE trip_id=?", (trip_id,))
            return energy

        if event_type == "arrived":
            connection.execute(
                "UPDATE reservation_stops SET status='arrived',is_fact=1,arrive_at=? WHERE res_stop_id=?",
                (format_time(moment), stop["res_stop_id"]))
            connection.execute("UPDATE trips SET status='in_progress' WHERE trip_id=? AND status='confirmed'",
                               (trip_id,))
            return energy

        if event_type in {"charging", "charge_progress"}:
            if delivered is not None:
                if delivered < stop["delivered_kwh"] - 1e-6:
                    raise ValidationError("累计充电量不能小于已记录值")
                connection.execute(
                    "UPDATE reservation_stops SET status='charging',is_fact=1,delivered_kwh=? "
                    "WHERE res_stop_id=?", (delivered, stop["res_stop_id"]))
            else:
                connection.execute(
                    "UPDATE reservation_stops SET status='charging',is_fact=1 WHERE res_stop_id=?",
                    (stop["res_stop_id"],))
            connection.execute("UPDATE trips SET status='in_progress' WHERE trip_id=?", (trip_id,))
            arrival_energy = self._stop_arrival_energy(connection, stop)
            return (arrival_energy + delivered) if arrival_energy is not None and delivered is not None else energy

        if event_type == "completed":
            if delivered is None:
                raise ValidationError("完成充电必须携带累计 delivered_kwh")
            if delivered < stop["delivered_kwh"] - 1e-6:
                raise ValidationError("累计充电量不能小于已记录值")
            arrival_energy = self._stop_arrival_energy(connection, stop)
            result_energy = (arrival_energy + delivered) if arrival_energy is not None else energy
            if result_energy is None:
                raise ValidationError("缺少到站电量基准，请在到达或完成时提供 energy_kwh")
            connection.execute(
                "UPDATE reservation_stops SET status='completed',is_fact=1,delivered_kwh=?,"
                "depart_at=? WHERE res_stop_id=?",
                (delivered, format_time(moment), stop["res_stop_id"]))
            self._release_ledger(connection, stop["res_stop_id"])
            return result_energy

        if event_type == "departed":
            connection.execute(
                "UPDATE reservation_stops SET status='departed',is_fact=1,depart_at=? WHERE res_stop_id=?",
                (format_time(moment), stop["res_stop_id"]))
            self._release_ledger(connection, stop["res_stop_id"])
            return energy if energy is not None else self._stop_arrival_energy(connection, stop)

        if event_type == "fault":
            if energy is None:
                raise ValidationError("故障改派必须携带车辆当前剩余电量 energy_kwh")
            connection.execute(
                "UPDATE reservation_stops SET status='faulted',is_fact=1 WHERE res_stop_id=?",
                (stop["res_stop_id"],))
            self._abandon_after_fault(connection, trip=trip, reservation=reservation,
                                      fault_stop=stop, energy=energy, moment=moment,
                                      reason="station_fault",
                                      detail={"message": detail.get("message"), "charger_id": charger_id},
                                      actor_id=actor_id)
            return energy

        if event_type == "queue_timeout":
            if energy is None:
                raise ValidationError("排队超时改派必须携带车辆当前剩余电量 energy_kwh")
            connection.execute(
                "UPDATE reservation_stops SET status='cancelled',is_fact=1 WHERE res_stop_id=?",
                (stop["res_stop_id"],))
            self._abandon_after_fault(connection, trip=trip, reservation=reservation,
                                      fault_stop=stop, energy=energy, moment=moment,
                                      reason="queue_timeout", detail={}, actor_id=actor_id)
            return energy

        raise ValidationError("event_type 不合法")

    def _stop_arrival_energy(self, connection, stop) -> float | None:
        row = connection.execute(
            "SELECT energy_kwh FROM trip_state_facts WHERE trip_id="
            "(SELECT trip_id FROM reservations WHERE reservation_id=?) AND station_id=? "
            "AND kind='arrived' ORDER BY fact_seq DESC LIMIT 1",
            (stop["reservation_id"], stop["station_id"])).fetchone()
        return row["energy_kwh"] if row and row["energy_kwh"] is not None else None

    def _release_ledger(self, connection, res_stop_id: str) -> None:
        """会话结束（完成/离场/故障）后释放该停靠站登记的全部时隙占用。"""

        connection.execute(
            "UPDATE reservation_slot_ledger SET active=0 WHERE res_stop_id=? AND active=1",
            (res_stop_id,))

    def _abandon_after_fault(self, connection, *, trip, reservation, fault_stop, energy: float,
                             moment: datetime, reason: str, detail: dict[str, Any],
                             actor_id: str) -> None:
        """故障/超时后：释放本站与后续未到站占用，任务转为待重规划。"""

        self._release_ledger(connection, fault_stop["res_stop_id"])
        connection.execute(
            "UPDATE reservation_slot_ledger SET active=0 WHERE reservation_id=? AND active=1 "
            "AND res_stop_id IN (SELECT res_stop_id FROM reservation_stops WHERE status='reserved')",
            (reservation["reservation_id"],))
        connection.execute(
            "UPDATE reservation_stops SET status='cancelled' WHERE reservation_id=? AND status='reserved'",
            (reservation["reservation_id"],))
        connection.execute("UPDATE reservations SET status='superseded' WHERE reservation_id=?",
                           (reservation["reservation_id"],))
        connection.execute("UPDATE plans SET status='superseded' WHERE plan_id=?",
                           (reservation["plan_id"],))
        connection.execute("UPDATE trips SET status='awaiting_replan' WHERE trip_id=?",
                           (trip["trip_id"],))
        node_id = self._station_node(connection, fault_stop["station_id"])
        self._record_replan(connection, trip_id=trip["trip_id"], reason=reason,
                            detail={"actor_id": actor_id, "station_id": fault_stop["station_id"],
                                    "node_id": node_id, "energy_kwh": energy,
                                    "occurred_at": format_time(moment), **detail},
                            from_plan_id=reservation["plan_id"], to_plan_id=None)
        append_event(connection, actor_id=actor_id, action=f"trip.replan_required:{reason}",
                     resource_type="trip", resource_id=trip["trip_id"],
                     detail={"station_id": fault_stop["station_id"], "energy_kwh": energy, **detail},
                     occurred_at=format_time(moment))

    # ================================================================ 重规划

    def replan(self, *, request_id: str, actor_id: str, trip_id: str, reason: str,
               current_energy_kwh: float | None = None, current_node: str | None = None,
               now_at: str | None = None, detail: dict[str, Any] | None = None) -> PlanView:
        """以已完成充电事实为锚点生成新计划；道路封闭等情况可显式给出电量与位置。"""

        payload = {"trip_id": trip_id, "reason": reason,
                   "current_energy_kwh": current_energy_kwh, "current_node": current_node,
                   "now_at": now_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            trip = self._trip(connection, trip_id)
            if reason not in {"station_fault", "queue_timeout", "road_closed", "early_arrival",
                              "dispatch_change", "capacity_change", "rescue_preemption"}:
                raise ValidationError("replan reason 不合法")

            def create() -> tuple[str, str, dict[str, Any]]:
                anchor = self._replan_anchor(connection, trip, reason,
                                             float(current_energy_kwh)
                                             if current_energy_kwh is not None else None,
                                             current_node,
                                             parse_time(now_at) if now_at else None)
                reservation = self._active_reservation(connection, trip_id)
                previous_plan_id = None
                if reservation is not None:
                    self._guard_started_sessions(connection, reservation, reason)
                    previous_plan_id = reservation["plan_id"]
                    connection.execute(
                        "UPDATE reservation_slot_ledger SET active=0 WHERE reservation_id=? AND active=1",
                        (reservation["reservation_id"],))
                    cancellable = ('reserved',) if reason != "early_arrival" else ('reserved', 'arrived')
                    placeholders = ",".join("?" for _ in cancellable)
                    connection.execute(
                        f"UPDATE reservation_stops SET status='cancelled' WHERE reservation_id=? "
                        f"AND status IN ({placeholders})",
                        (reservation["reservation_id"], *cancellable))
                    connection.execute("UPDATE reservations SET status='superseded' WHERE reservation_id=?",
                                       (reservation["reservation_id"],))
                    connection.execute("UPDATE plans SET status='superseded' WHERE plan_id=?",
                                       (reservation["plan_id"],))
                network = self.get_road_network(None)
                route, equipment, names = self._route_context(connection, trip, network, anchor["node_id"])
                result = plan_with_fallback(
                    route=route, station_equipment=equipment,
                    initial_energy_kwh=anchor["energy_kwh"], battery_kwh=trip["battery_kwh"],
                    max_charge_kw=trip["max_charge_kw"], rate_kwh_per_km=trip["rate_kwh_per_km"],
                    reserve_km=trip["reserve_km"], depart_at=anchor["at"],
                    arrive_by=parse_time(trip["arrive_by"]),
                    capacity_fn=self._proposal_capacity_fn(
                        connection, None, rescue=trip["priority"] == "rescue"),
                    station_names=names)
                plan_id = self._persist_plan(connection, trip=trip, network=network, result=result,
                                             route=route, equipment=equipment, anchor=anchor)
                connection.execute("UPDATE trips SET status='awaiting_replan' WHERE trip_id=?",
                                   (trip_id,))
                self._record_replan(connection, trip_id=trip_id, reason=reason,
                                    detail={"actor_id": actor_id,
                                            "anchor": {**anchor, "at": format_time(anchor["at"])},
                                            "previous_road_version": trip["road_version"],
                                            "road_version": network.version,
                                            "feasible": result.feasible, "reason_text": result.reason,
                                            **(detail or {})},
                                    from_plan_id=previous_plan_id, to_plan_id=plan_id)
                append_event(connection, actor_id=actor_id, action="trip.replanned",
                             resource_type="plan", resource_id=plan_id,
                             detail={"trip_id": trip_id, "reason": reason,
                                     "feasible": result.feasible,
                                     "anchor": {**anchor, "at": format_time(anchor["at"])}},
                             occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id}

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="replan", payload=payload, create=create)
            return self._load_plan(connection, receipt.resource_id)

    def _guard_started_sessions(self, connection, reservation, reason: str = "") -> None:
        if reason == "early_arrival":
            # 提前到达允许以"已到站未开始充电"的事实为锚点重排，但已开始充电仍受保护
            blocked_statuses = ("charging",)
        else:
            blocked_statuses = ("arrived", "charging")
        placeholders = ",".join("?" for _ in blocked_statuses)
        row = connection.execute(
            f"SELECT COUNT(*) AS count FROM reservation_stops WHERE reservation_id=? "
            f"AND status IN ({placeholders})",
            (reservation["reservation_id"], *blocked_statuses)).fetchone()
        if row["count"]:
            raise PlanningError("存在已开始充电的会话，抢险优先权与改派都不能挤掉它；"
                                "请先登记完成或故障事实后再改派")

    def _replan_anchor(self, connection, trip, reason: str, energy: float | None,
                       node: str | None, at: datetime | None) -> dict[str, Any]:
        """从事实台账推算重规划起点（位置、时间、真实剩余电量）。"""

        fact = connection.execute(
            "SELECT * FROM trip_state_facts WHERE trip_id=? ORDER BY fact_seq DESC LIMIT 1",
            (trip["trip_id"],)).fetchone()
        now = at or self.clock.now()
        if fact is not None and fact["kind"] in {"fault", "queue_timeout"}:
            return {"node_id": fact["node_id"], "energy_kwh": fact["energy_kwh"],
                    "at": parse_time(fact["occurred_at"]), "basis": f"fact:{fact['kind']}"}
        if fact is not None and reason == "early_arrival" and fact["kind"] == "arrived":
            if fact["energy_kwh"] is None:
                raise ValidationError("提前到达事实缺少到站电量，请在 arrived 事件中携带 energy_kwh")
            return {"node_id": fact["node_id"], "energy_kwh": fact["energy_kwh"],
                    "at": parse_time(fact["occurred_at"]), "basis": "fact:arrived"}
        terminal = connection.execute(
            "SELECT * FROM trip_state_facts WHERE trip_id=? AND kind IN ('completed','departed') "
            "ORDER BY fact_seq DESC LIMIT 1", (trip["trip_id"],)).fetchone()
        if terminal is not None:
            anchor_node = node or terminal["node_id"]
            anchor_energy = energy if energy is not None else terminal["energy_kwh"]
            if anchor_energy is None:
                raise ValidationError("缺少当前电量遥测（current_energy_kwh）")
            if anchor_node not in self.get_road_network(None).nodes:
                raise ValidationError("current_node 不在当前道路版本中")
            return {"node_id": anchor_node, "energy_kwh": anchor_energy, "at": now,
                    "basis": f"fact:{terminal['kind']}+telemetry"}
        # 尚未产生任何到站事实：道路封闭/调度变更可能发生在行驶途中
        anchor_energy = energy if energy is not None else trip["initial_energy_kwh"]
        anchor_node = node or trip["origin_node"]
        network = self.get_road_network(None)
        if anchor_node not in network.nodes:
            raise ValidationError("current_node 不在当前道路版本中")
        depart_at = parse_time(trip["depart_at"])
        return {"node_id": anchor_node, "energy_kwh": anchor_energy,
                "at": max(now, depart_at), "basis": "telemetry" if energy is not None else "initial"}

    def _persist_plan(self, connection, *, trip, network, result, route, equipment, anchor) -> str:
        version_seq = (connection.execute(
            "SELECT COALESCE(MAX(version_seq),0)+1 AS seq FROM plans WHERE trip_id=?",
            (trip["trip_id"],)).fetchone())["seq"]
        connection.execute("UPDATE plans SET status='superseded' WHERE trip_id=? AND status='proposed'",
                           (trip["trip_id"],))
        plan_id = uuid.uuid4().hex
        now = self.clock.now()
        station_ids = [s.station_id for s in result.stops]
        station_digests = {sid: equipment[sid].digest for sid in station_ids}
        metrics = {"destination_arrive_at": format_time(result.destination_arrive_at)
                   if result.destination_arrive_at else None,
                   "destination_margin_km": round(result.destination_margin_km, 3),
                   "total_charge_kwh": round(result.total_charge_kwh, 3),
                   "total_wait_minutes": round(result.total_wait_minutes, 2),
                   "anchor_basis": anchor["basis"]}
        summary = {"infeasible_reason": result.reason, "metrics": metrics,
                   "stations": station_ids, "station_digests": station_digests,
                   "route_node_ids": [p.node_id for p in route],
                   "start_node": anchor["node_id"], "start_energy_kwh": anchor["energy_kwh"],
                   "start_at": format_time(anchor["at"])}
        connection.execute(
            "INSERT INTO plans(plan_id,trip_id,version_seq,status,feasible,road_version,"
            "equipment_digest,valid_from,valid_until,summary_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, trip["trip_id"], version_seq, "proposed", 1 if result.feasible else 0,
             network.version, digest(station_digests), format_time(now),
             format_time(now + timedelta(minutes=self.plan_ttl_minutes)),
             canonical_json(summary), format_time(now)))
        for stop in result.stops:
            connection.execute(
                "INSERT INTO plan_stops(stop_id,plan_id,seq,station_id,arrive_at,depart_at,"
                "slot_start,slot_end,charge_kwh,planned_power_kw,arrive_range_km,reserve_margin_km) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, plan_id, stop.seq, stop.station_id,
                 format_time(stop.arrive_at), format_time(stop.depart_at),
                 format_time(stop.charge_start), format_time(stop.depart_at),
                 stop.charge_kwh, stop.planned_power_kw, stop.arrive_range_km,
                 stop.reserve_margin_km))
        return plan_id

    # ------------------------------------------------------------ 超时扫描

    def sweep_queue_timeouts(self, *, now_at: str | None = None) -> list[dict[str, Any]]:
        """把超过站点排队时限仍未开始服务的预约标记为超时并释放容量。"""

        by_trip: dict[str, dict[str, Any]] = {}
        now = parse_time(now_at) if now_at else self.clock.now()
        with self.database.transaction(immediate=True) as connection:
            pending = connection.execute(
                "SELECT rs.*, st.queue_timeout_minutes FROM reservation_stops rs "
                "JOIN charging_stations st ON st.station_id=rs.station_id "
                "JOIN reservations r ON r.reservation_id=rs.reservation_id "
                "WHERE rs.status='reserved' AND r.status='active' AND rs.is_fact=0").fetchall()
            for stop in pending:
                deadline = parse_time(stop["arrive_at"]) + timedelta(
                    minutes=stop["queue_timeout_minutes"])
                if now < deadline:
                    continue
                current = connection.execute(
                    "SELECT status FROM reservation_stops WHERE res_stop_id=?",
                    (stop["res_stop_id"],)).fetchone()
                if current["status"] != "reserved":
                    continue
                self._sweep_one(connection, stop=stop, now=now)
                trip_id = connection.execute(
                    "SELECT trip_id FROM reservations WHERE reservation_id=?",
                    (stop["reservation_id"],)).fetchone()["trip_id"]
                item = by_trip.setdefault(
                    trip_id, {"trip_id": trip_id, "timed_out_stations": [],
                              "deadlines": []})
                item["timed_out_stations"].append(stop["station_id"])
                item["deadlines"].append(format_time(deadline))
        return list(by_trip.values())

    def _sweep_one(self, connection, *, stop, now: datetime) -> None:
        reservation = connection.execute("SELECT * FROM reservations WHERE reservation_id=?",
                                         (stop["reservation_id"],)).fetchone()
        trip = self._trip(connection, reservation["trip_id"])
        node_id = self._station_node(connection, stop["station_id"])
        connection.execute(
            "UPDATE reservation_stops SET status='cancelled',is_fact=1 WHERE res_stop_id=?",
            (stop["res_stop_id"],))
        self._insert_fact(connection, trip_id=trip["trip_id"], kind="queue_timeout",
                          node_id=node_id, station_id=stop["station_id"],
                          occurred_at=now, detail={"stop_seq": stop["seq"], "automatic": True})
        connection.execute(
            "UPDATE reservation_slot_ledger SET active=0 WHERE reservation_id=? AND active=1 "
            "AND res_stop_id IN (SELECT res_stop_id FROM reservation_stops WHERE status='cancelled')",
            (stop["reservation_id"],))
        connection.execute(
            "UPDATE reservation_slot_ledger SET active=0 WHERE reservation_id=? AND active=1 "
            "AND res_stop_id IN (SELECT res_stop_id FROM reservation_stops WHERE status='reserved')",
            (stop["reservation_id"],))
        connection.execute(
            "UPDATE reservation_stops SET status='cancelled' WHERE reservation_id=? AND status='reserved'",
            (stop["reservation_id"],))
        connection.execute("UPDATE reservations SET status='superseded' WHERE reservation_id=?",
                           (stop["reservation_id"],))
        connection.execute("UPDATE plans SET status='superseded' WHERE plan_id=?",
                           (reservation["plan_id"],))
        connection.execute("UPDATE trips SET status='awaiting_replan' WHERE trip_id=?",
                           (trip["trip_id"],))
        self._record_replan(connection, trip_id=trip["trip_id"], reason="queue_timeout",
                            detail={"actor_id": "system", "station_id": stop["station_id"],
                                    "node_id": node_id, "occurred_at": format_time(now),
                                    "automatic": True, "needs_telemetry": True},
                            from_plan_id=reservation["plan_id"], to_plan_id=None)
        append_event(connection, actor_id="system", action="trip.replan_required:queue_timeout",
                     resource_type="trip", resource_id=trip["trip_id"],
                     detail={"station_id": stop["station_id"]}, occurred_at=format_time(now))

    # ================================================================ 运营查询

    def _anchor_readonly(self, connection, trip) -> dict[str, Any]:
        """只读方式推算任务当前位置、时间与真实电量（依据事实台账）。"""

        fact = connection.execute(
            "SELECT * FROM trip_state_facts WHERE trip_id=? ORDER BY fact_seq DESC LIMIT 1",
            (trip["trip_id"],)).fetchone()
        if fact is not None and fact["kind"] in {"fault", "queue_timeout", "arrived"} \
                and fact["energy_kwh"] is not None:
            return {"node_id": fact["node_id"], "energy_kwh": fact["energy_kwh"],
                    "at": parse_time(fact["occurred_at"]), "basis": f"fact:{fact['kind']}"}
        terminal = connection.execute(
            "SELECT * FROM trip_state_facts WHERE trip_id=? AND kind IN ('completed','departed') "
            "ORDER BY fact_seq DESC LIMIT 1", (trip["trip_id"],)).fetchone()
        if terminal is not None and terminal["energy_kwh"] is not None:
            return {"node_id": terminal["node_id"], "energy_kwh": terminal["energy_kwh"],
                    "at": parse_time(terminal["occurred_at"]),
                    "basis": f"fact:{terminal['kind']}"}
        delivered = connection.execute(
            "SELECT COALESCE(SUM(delivered_kwh),0) AS total FROM reservation_stops WHERE "
            "reservation_id IN (SELECT reservation_id FROM reservations WHERE trip_id=?) AND is_fact=1",
            (trip["trip_id"],)).fetchone()["total"]
        # 无位置事实时以起点+初始电量为保守基准（实际位置由后续遥测事实修正）
        return {"node_id": trip["origin_node"],
                "energy_kwh": min(trip["battery_kwh"], trip["initial_energy_kwh"] + delivered),
                "at": parse_time(trip["depart_at"]), "basis": "initial+delivered"}

    def safe_arrivals(self) -> list[dict[str, Any]]:
        """判断每趟在途任务能否安全抵达下一预约站，并给出可达站点列表。"""

        connection = self.database.connection
        try:
            network = self.get_road_network(None)
        except NotFoundError:
            return []
        items: list[dict[str, Any]] = []
        trips = connection.execute(
            "SELECT * FROM trips WHERE status IN ('confirmed','in_progress','awaiting_replan')"
        ).fetchall()
        for trip in trips:
            anchor = self._anchor_readonly(connection, trip)
            usable_range_km = anchor["energy_kwh"] / trip["rate_kwh_per_km"] - trip["reserve_km"]
            reachable: list[dict[str, Any]] = []
            next_station = None
            reservation = self._active_reservation(connection, trip["trip_id"])
            if reservation is not None:
                next_stop = connection.execute(
                    "SELECT * FROM reservation_stops WHERE reservation_id=? AND status='reserved' "
                    "ORDER BY seq LIMIT 1", (reservation["reservation_id"],)).fetchone()
            else:
                next_stop = None
            station_rows = connection.execute("SELECT * FROM charging_stations").fetchall()
            for station in station_rows:
                route = shortest_route(network, anchor["node_id"], station["node_id"])
                if not route:
                    continue
                distance = route[-1].distance_km
                if distance <= usable_range_km + 1e-6:
                    reachable.append({"station_id": station["station_id"], "distance_km": round(distance, 2)})
                if next_stop is not None and station["station_id"] == next_stop["station_id"]:
                    next_station = {"station_id": station["station_id"], "distance_km": round(distance, 2),
                                    "safe": distance <= usable_range_km + 1e-6,
                                    "margin_km": round(usable_range_km - distance, 2)}
            dest_route = shortest_route(network, anchor["node_id"], trip["destination_node"])
            destination = None
            if dest_route:
                destination = {"distance_km": round(dest_route[-1].distance_km, 2),
                               "reachable_without_charge":
                                   dest_route[-1].distance_km <= usable_range_km + 1e-6}
            reachable.sort(key=lambda item: item["distance_km"])
            items.append({"trip_id": trip["trip_id"], "vehicle_id": trip["vehicle_id"],
                          "priority": trip["priority"], "status": trip["status"],
                          "anchor": {**anchor, "energy_kwh": round(anchor["energy_kwh"], 3),
                                     "at": format_time(anchor["at"])},
                          "usable_range_km": round(max(0.0, usable_range_km), 2),
                          "next_reserved_station": next_station,
                          "can_reach_next_station": bool(next_station and next_station["safe"]),
                          "reachable_stations": reachable[:10],
                          "destination": destination})
        return items

    def station_capacity(self, station_id: str, *, from_at: str | None = None,
                         slots: int = 16) -> dict[str, Any]:
        """返回站点各 15 分钟时隙的真实可用功率（扣减在运桩检修与分时限额）。"""

        start = slot_floor(parse_time(from_at)) if from_at else slot_floor(self.clock.now())
        with self.database.transaction() as connection:
            equipment = self.get_station_equipment(station_id)
            maintenance = [c["charger_id"] for c in equipment.chargers
                           if c["status"] in {"maintenance", "faulted"}]
            windows = []
            for index in range(slots):
                slot = start + timedelta(minutes=SLOT_MINUTES * index)
                view = self._capacity_view(connection, station_id, slot)
                windows.append({"slot_start": format_time(slot),
                                "slot_end": format_time(slot + timedelta(minutes=SLOT_MINUTES)),
                                "operational_chargers": int(view["cap_chargers"]),
                                "power_limit_kw": round(view["cap_power"], 2),
                                "used_sessions": int(view["used_chargers"]),
                                "used_power_kw": round(view["used_power"], 2),
                                "available_chargers": int(view["available_chargers"]),
                                "available_power_kw": round(view["available_power"], 2)})
            return {"station_id": station_id, "name": equipment.name,
                    "queue_timeout_minutes": equipment.queue_timeout_minutes,
                    "maintenance_chargers": maintenance, "slots": windows}

    def list_replans(self, trip_id: str) -> list[ReplanView]:
        """给出一趟任务的完整改派历史（解释每次改派为何发生）。"""

        connection = self.database.connection
        if connection.execute("SELECT 1 FROM trips WHERE trip_id=?", (trip_id,)).fetchone() is None:
            raise NotFoundError("运输任务不存在")
        views = []
        for row in connection.execute("SELECT * FROM replans WHERE trip_id=? ORDER BY seq", (trip_id,)):
            views.append(ReplanView(replan_id=row["replan_id"], trip_id=trip_id, seq=row["seq"],
                                    reason=row["reason"], detail=json.loads(row["detail_json"]),
                                    from_plan_id=row["from_plan_id"], to_plan_id=row["to_plan_id"],
                                    created_at=row["created_at"]))
        return views

    def trip_overview(self, trip_id: str) -> dict[str, Any]:
        """汇总任务、当前预约、事实序列与最新计划，供运营席位一次取全。"""

        connection = self.database.connection
        trip = self._trip(connection, trip_id)
        reservation = self._active_reservation(connection, trip_id)
        facts = [{"fact_id": row["fact_id"], "kind": row["kind"], "station_id": row["station_id"],
                  "energy_kwh": row["energy_kwh"], "delivered_kwh": row["delivered_kwh"],
                  "occurred_at": row["occurred_at"], "detail": json.loads(row["detail_json"])}
                 for row in connection.execute(
                     "SELECT * FROM trip_state_facts WHERE trip_id=? ORDER BY fact_seq", (trip_id,))]
        latest_plan = connection.execute(
            "SELECT plan_id FROM plans WHERE trip_id=? ORDER BY version_seq DESC LIMIT 1",
            (trip_id,)).fetchone()
        return {"trip_id": trip_id, "vehicle_id": trip["vehicle_id"], "priority": trip["priority"],
                "status": trip["status"], "road_version": trip["road_version"],
                "reservation": self._load_reservation(connection,
                                                      reservation["reservation_id"]).__dict__
                if reservation else None,
                "latest_plan_id": latest_plan["plan_id"] if latest_plan else None,
                "facts": facts,
                "replan_count": connection.execute(
                    "SELECT COUNT(*) AS count FROM replans WHERE trip_id=?", (trip_id,)).fetchone()["count"]}
