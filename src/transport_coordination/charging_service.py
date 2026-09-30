"""干线充电保障领域服务。

在基础服务的权限、幂等、SQLite 事务与审计链之上，提供车辆与任务建档、
补能计划生成、多站时隙原子锁定、充电会话事实、事件重规划和运营查询。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import charging
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .storage import Database

PLAN_VALID_MINUTES = 30
REPLAN_VALID_MINUTES = 20
ARRIVAL_TOLERANCE_MINUTES = 30

EVENT_KINDS = frozenset({
    "fault", "queue_timeout", "early_arrival", "road_closure", "rescue_displaced",
    "plan_confirmed", "plan_replanned", "arrival", "session_started", "session_completed",
})


def parse_dt(value: str | datetime, field: str = "时间") -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} 不是合法 ISO 时间") from exc
    if moment.tzinfo is None:
        raise ValidationError(f"{field} 必须带时区")
    return moment.astimezone(timezone.utc)


def fmt_dt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ChargingService:
    """实现干线充电保障的全部写入与查询用例。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> datetime:
        return self.clock.now()

    def _now_text(self) -> str:
        return fmt_dt(self._now())

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        request_id = str(request_id).strip()
        if not request_id:
            raise ValidationError("request_id 不能为空")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            result = json.loads(row["response_json"])
            result["replayed"] = True
            return result
        resource_type, resource_id, response = create()
        response = dict(response)
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now_text()),
        )
        response["replayed"] = False
        return response

    def _actor(self, connection, actor_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return {"actor_id": row["actor_id"], "role": row["role"],
                "organization_id": row["organization_id"]}

    def _require_staff(self, actor: dict[str, Any]) -> None:
        if actor["role"] not in ("admin", "operator"):
            raise PermissionDenied("当前角色不能执行该动作")

    def _require_trip_actor(self, connection, actor: dict[str, Any], trip: Any) -> None:
        if actor["role"] in ("admin", "operator"):
            return
        if trip["driver_actor_id"] and actor["actor_id"] == trip["driver_actor_id"]:
            return
        raise PermissionDenied("只能操作分配给本人的运输任务")

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now_text())

    # ------------------------------------------------------------------ 网络建档

    def register_vehicle(self, *, request_id: str, actor_id: str, vehicle_id: str,
                         display_name: str, battery_kwh: float, kwh_per_km_empty: float,
                         kwh_per_km_loaded: float, rated_payload_tonnes: float,
                         reserve_kwh: float, priority: str = "normal") -> dict[str, Any]:
        payload = {"vehicle_id": vehicle_id, "battery_kwh": battery_kwh,
                   "kwh_per_km_empty": kwh_per_km_empty, "kwh_per_km_loaded": kwh_per_km_loaded,
                   "rated_payload_tonnes": rated_payload_tonnes, "reserve_kwh": reserve_kwh,
                   "priority": priority}
        self._positive(battery_kwh, "battery_kwh")
        self._positive(kwh_per_km_empty, "kwh_per_km_empty")
        if kwh_per_km_loaded < kwh_per_km_empty:
            raise ValidationError("满载能耗不能低于空载能耗")
        self._positive(rated_payload_tonnes, "rated_payload_tonnes")
        if reserve_kwh < 0 or reserve_kwh >= battery_kwh:
            raise ValidationError("reserve_kwh 必须位于 [0, battery_kwh)")
        if priority not in ("normal", "rescue"):
            raise ValidationError("priority 只能是 normal 或 rescue")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_staff(actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO vehicles(vehicle_id,organization_id,display_name,battery_kwh,"
                        "kwh_per_km_empty,kwh_per_km_loaded,rated_payload_tonnes,reserve_kwh,"
                        "priority,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (vehicle_id, actor["organization_id"], display_name, battery_kwh,
                         kwh_per_km_empty, kwh_per_km_loaded, rated_payload_tonnes, reserve_kwh,
                         priority, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("车辆编号已经存在或组织无效") from exc
                self._audit(conn, actor_id=actor_id, action="vehicle.registered",
                            resource_type="vehicle", resource_id=vehicle_id, detail=payload)
                return "vehicle", vehicle_id, {"vehicle_id": vehicle_id}

            return self._idempotent(conn, request_id=request_id, action="register_vehicle",
                                    payload={"actor_id": actor_id, **payload}, create=create)

    def register_corridor(self, *, request_id: str, actor_id: str, corridor_id: str,
                          display_name: str, length_km: float, avg_speed_kmh: float) -> dict[str, Any]:
        self._positive(length_km, "length_km")
        self._positive(avg_speed_kmh, "avg_speed_kmh")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_staff(actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO corridors(corridor_id,organization_id,display_name,"
                        "length_km,avg_speed_kmh,created_at) VALUES(?,?,?,?,?,?)",
                        (corridor_id, actor["organization_id"], display_name, length_km,
                         avg_speed_kmh, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("干线编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="corridor.registered",
                            resource_type="corridor", resource_id=corridor_id,
                            detail={"display_name": display_name, "length_km": length_km})
                return "corridor", corridor_id, {"corridor_id": corridor_id}

            return self._idempotent(conn, request_id=request_id, action="register_corridor",
                                    payload={"actor_id": actor_id, "corridor_id": corridor_id,
                                             "length_km": length_km,
                                             "avg_speed_kmh": avg_speed_kmh}, create=create)

    def register_station(self, *, request_id: str, actor_id: str, station_id: str,
                         corridor_id: str, display_name: str, position_km: float,
                         active: bool = True) -> dict[str, Any]:
        if position_km < 0:
            raise ValidationError("position_km 不能为负")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_staff(actor)
            self._require_corridor_org(conn, actor, corridor_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO charging_stations(station_id,corridor_id,display_name,"
                        "position_km,active,version,created_at) VALUES(?,?,?,?,?,1,?)",
                        (station_id, corridor_id, display_name, position_km,
                         1 if active else 0, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("站点编号已经存在或桩位冲突") from exc
                self._audit(conn, actor_id=actor_id, action="station.registered",
                            resource_type="charging_station", resource_id=station_id,
                            detail={"corridor_id": corridor_id, "position_km": position_km,
                                    "active": bool(active)})
                return "charging_station", station_id, {"station_id": station_id}

            return self._idempotent(conn, request_id=request_id, action="register_station",
                                    payload={"actor_id": actor_id, "station_id": station_id,
                                             "corridor_id": corridor_id, "position_km": position_km,
                                             "active": bool(active)}, create=create)

    def set_station_active(self, *, request_id: str, actor_id: str, station_id: str,
                           active: bool) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_staff(actor)
            station = self._station(conn, station_id)
            self._require_corridor_org(conn, actor, station["corridor_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE charging_stations SET active=?, version=version+1 WHERE station_id=?",
                             (1 if active else 0, station_id))
                self._audit(conn, actor_id=actor_id, action="station.updated",
                            resource_type="charging_station", resource_id=station_id,
                            detail={"active": bool(active)})
                return "charging_station", station_id, {"station_id": station_id,
                                                         "active": bool(active)}

            return self._idempotent(conn, request_id=request_id, action="set_station_active",
                                    payload={"actor_id": actor_id, "station_id": station_id,
                                             "active": bool(active)}, create=create)

    def register_charger(self, *, request_id: str, actor_id: str, charger_id: str,
                         station_id: str, rated_power_kw: float, active: bool = True) -> dict[str, Any]:
        self._positive(rated_power_kw, "rated_power_kw")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_staff(actor)
            station = self._station(conn, station_id)
            self._require_corridor_org(conn, actor, station["corridor_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO chargers(charger_id,station_id,rated_power_kw,active,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (charger_id, station_id, rated_power_kw, 1 if active else 0,
                         self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("充电桩编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="charger.registered",
                            resource_type="charger", resource_id=charger_id,
                            detail={"station_id": station_id, "rated_power_kw": rated_power_kw})
                return "charger", charger_id, {"charger_id": charger_id}

            return self._idempotent(conn, request_id=request_id, action="register_charger",
                                    payload={"actor_id": actor_id, "charger_id": charger_id,
                                             "station_id": station_id,
                                             "rated_power_kw": rated_power_kw,
                                             "active": bool(active)}, create=create)

    def add_station_outage(self, *, request_id: str, actor_id: str, station_id: str,
                           starts_at: str, ends_at: str, reason: str) -> dict[str, Any]:
        start = parse_dt(starts_at, "starts_at")
        end = parse_dt(ends_at, "ends_at")
        if end <= start:
            raise ValidationError("检修结束时间必须晚于开始时间")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_staff(actor)
            station = self._station(conn, station_id)
            self._require_corridor_org(conn, actor, station["corridor_id"])
            conn.execute("UPDATE charging_stations SET version=version+1 WHERE station_id=?",
                         (station_id,))

            def create() -> tuple[str, str, dict[str, Any]]:
                outage_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO station_outages(outage_id,station_id,starts_at,ends_at,reason,"
                    "created_at) VALUES(?,?,?,?,?,?)",
                    (outage_id, station_id, fmt_dt(start), fmt_dt(end), reason, self._now_text()),
                )
                self._audit(conn, actor_id=actor_id, action="station_outage.added",
                            resource_type="station_outage", resource_id=outage_id,
                            detail={"station_id": station_id, "starts_at": fmt_dt(start),
                                    "ends_at": fmt_dt(end), "reason": reason})
                return "station_outage", outage_id, {"outage_id": outage_id}

            return self._idempotent(conn, request_id=request_id, action="add_station_outage",
                                    payload={"actor_id": actor_id, "station_id": station_id,
                                             "starts_at": fmt_dt(start), "ends_at": fmt_dt(end),
                                             "reason": reason}, create=create)

    def add_charger_outage(self, *, request_id: str, actor_id: str, charger_id: str,
                           starts_at: str, ends_at: str, reason: str) -> dict[str, Any]:
        start = parse_dt(starts_at, "starts_at")
        end = parse_dt(ends_at, "ends_at")
        if end <= start:
            raise ValidationError("检修结束时间必须晚于开始时间")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_staff(actor)
            row = conn.execute("SELECT * FROM chargers WHERE charger_id=?", (charger_id,)).fetchone()
            if row is None:
                raise NotFoundError("充电桩不存在")
            station = self._station(conn, row["station_id"])
            self._require_corridor_org(conn, actor, station["corridor_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                outage_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO charger_outages(outage_id,charger_id,starts_at,ends_at,reason,"
                    "created_at) VALUES(?,?,?,?,?,?)",
                    (outage_id, charger_id, fmt_dt(start), fmt_dt(end), reason, self._now_text()),
                )
                self._audit(conn, actor_id=actor_id, action="charger_outage.added",
                            resource_type="charger_outage", resource_id=outage_id,
                            detail={"charger_id": charger_id, "starts_at": fmt_dt(start),
                                    "ends_at": fmt_dt(end), "reason": reason})
                return "charger_outage", outage_id, {"outage_id": outage_id}

            return self._idempotent(conn, request_id=request_id, action="add_charger_outage",
                                    payload={"actor_id": actor_id, "charger_id": charger_id,
                                             "starts_at": fmt_dt(start), "ends_at": fmt_dt(end),
                                             "reason": reason}, create=create)

    def add_power_window(self, *, request_id: str, actor_id: str, station_id: str,
                         starts_at: str, ends_at: str, cap_kw: float, note: str = "") -> dict[str, Any]:
        start = parse_dt(starts_at, "starts_at")
        end = parse_dt(ends_at, "ends_at")
        if end <= start:
            raise ValidationError("功率窗口结束时间必须晚于开始时间")
        if cap_kw < 0:
            raise ValidationError("cap_kw 不能为负")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_staff(actor)
            station = self._station(conn, station_id)
            self._require_corridor_org(conn, actor, station["corridor_id"])
            conn.execute("UPDATE charging_stations SET version=version+1 WHERE station_id=?",
                         (station_id,))

            def create() -> tuple[str, str, dict[str, Any]]:
                window_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO power_windows(window_id,station_id,starts_at,ends_at,cap_kw,note,"
                    "created_at) VALUES(?,?,?,?,?,?,?)",
                    (window_id, station_id, fmt_dt(start), fmt_dt(end), cap_kw, note,
                     self._now_text()),
                )
                self._audit(conn, actor_id=actor_id, action="power_window.added",
                            resource_type="power_window", resource_id=window_id,
                            detail={"station_id": station_id, "starts_at": fmt_dt(start),
                                    "ends_at": fmt_dt(end), "cap_kw": cap_kw})
                return "power_window", window_id, {"window_id": window_id}

            return self._idempotent(conn, request_id=request_id, action="add_power_window",
                                    payload={"actor_id": actor_id, "station_id": station_id,
                                             "starts_at": fmt_dt(start), "ends_at": fmt_dt(end),
                                             "cap_kw": cap_kw, "note": note}, create=create)

    def publish_road_version(self, *, request_id: str, actor_id: str, corridor_id: str,
                             version: int, segments: list[dict[str, Any]],
                             note: str = "") -> dict[str, Any]:
        if not isinstance(version, int) or version < 1:
            raise ValidationError("version 必须是正整数")
        if not segments:
            raise ValidationError("道路版本至少包含一个路段")
        cleaned: list[tuple[float, float, int, float, str]] = []
        for item in segments:
            from_km = float(item["from_km"])
            to_km = float(item["to_km"])
            if to_km <= from_km:
                raise ValidationError("路段 to_km 必须大于 from_km")
            detour = float(item.get("detour_extra_km", 0.0))
            if detour < 0:
                raise ValidationError("detour_extra_km 不能为负")
            open_flag = 1 if item.get("open", True) else 0
            cleaned.append((from_km, to_km, open_flag, detour, str(item.get("note", ""))))
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_staff(actor)
            self._require_corridor_org(conn, actor, corridor_id)
            existing = conn.execute(
                "SELECT MAX(version) AS v FROM road_versions WHERE corridor_id=?", (corridor_id,)
            ).fetchone()["v"]
            if existing is not None and version <= existing:
                raise ConflictError("道路版本必须递增发布")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "INSERT INTO road_versions(corridor_id,version,note,published_at) "
                    "VALUES(?,?,?,?)",
                    (corridor_id, version, note, self._now_text()),
                )
                for from_km, to_km, open_flag, detour, seg_note in cleaned:
                    conn.execute(
                        "INSERT INTO road_segments(corridor_id,version,from_km,to_km,open,"
                        "detour_extra_km,note) VALUES(?,?,?,?,?,?,?)",
                        (corridor_id, version, from_km, to_km, open_flag, detour, seg_note),
                    )
                self._audit(conn, actor_id=actor_id, action="road_version.published",
                            resource_type="road_version", resource_id=f"{corridor_id}:{version}",
                            detail={"corridor_id": corridor_id, "version": version,
                                    "segments": len(cleaned), "note": note})
                return "road_version", f"{corridor_id}:{version}", {"corridor_id": corridor_id,
                                                                    "version": version}

            return self._idempotent(conn, request_id=request_id, action="publish_road_version",
                                    payload={"actor_id": actor_id, "corridor_id": corridor_id,
                                             "version": version, "segments": cleaned,
                                             "note": note}, create=create)

    # ------------------------------------------------------------------ 运输任务

    def create_trip(self, *, request_id: str, actor_id: str, trip_id: str, corridor_id: str,
                    vehicle_id: str, origin_km: float, destination_km: float, load_tonnes: float,
                    initial_energy_kwh: float, deadline: str, departure_at: str,
                    road_version: int | None = None, priority: str | None = None,
                    driver_actor_id: str | None = None) -> dict[str, Any]:
        if destination_km <= origin_km:
            raise ValidationError("destination_km 必须大于 origin_km")
        if load_tonnes < 0:
            raise ValidationError("load_tonnes 不能为负")
        deadline_dt = parse_dt(deadline, "deadline")
        departure_dt = parse_dt(departure_at, "departure_at")
        if deadline_dt <= departure_dt:
            raise ValidationError("任务时限必须晚于发车时间")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_staff(actor)
            vehicle = self._vehicle(conn, vehicle_id)
            corridor = self._corridor(conn, corridor_id)
            self._require_corridor_org(conn, actor, corridor_id)
            if vehicle["organization_id"] != actor["organization_id"] and actor["role"] != "admin":
                raise PermissionDenied("不能调度其他组织的车辆")
            if initial_energy_kwh <= 0 or initial_energy_kwh > vehicle["battery_kwh"]:
                raise ValidationError("初始电量必须位于 (0, battery_kwh]")
            if road_version is None:
                road_version = conn.execute(
                    "SELECT MAX(version) AS v FROM road_versions WHERE corridor_id=?", (corridor_id,)
                ).fetchone()["v"]
                if road_version is None:
                    raise ValidationError("干线尚未发布道路通行版本")
            else:
                row = conn.execute(
                    "SELECT 1 FROM road_versions WHERE corridor_id=? AND version=?",
                    (corridor_id, road_version),
                ).fetchone()
                if row is None:
                    raise NotFoundError("指定的道路通行版本不存在")
            trip_priority = priority or vehicle["priority"]
            if trip_priority not in ("normal", "rescue"):
                raise ValidationError("priority 只能是 normal 或 rescue")
            if driver_actor_id:
                self._actor(conn, driver_actor_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    conn.execute(
                        "INSERT INTO trips(trip_id,corridor_id,vehicle_id,driver_actor_id,origin_km,"
                        "destination_km,load_tonnes,initial_energy_kwh,deadline,departure_at,"
                        "road_version,priority,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (trip_id, corridor_id, vehicle_id, driver_actor_id, origin_km,
                         destination_km, load_tonnes, initial_energy_kwh, fmt_dt(deadline_dt),
                         fmt_dt(departure_dt), road_version, trip_priority, "planned",
                         self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("运输任务编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="trip.created",
                            resource_type="trip", resource_id=trip_id,
                            detail={"corridor_id": corridor_id, "vehicle_id": vehicle_id,
                                    "priority": trip_priority, "road_version": road_version})
                return "trip", trip_id, {"trip_id": trip_id, "road_version": road_version,
                                         "priority": trip_priority}

            return self._idempotent(conn, request_id=request_id, action="create_trip",
                                    payload={"actor_id": actor_id, "trip_id": trip_id,
                                             "corridor_id": corridor_id, "vehicle_id": vehicle_id,
                                             "origin_km": origin_km,
                                             "destination_km": destination_km,
                                             "load_tonnes": load_tonnes,
                                             "initial_energy_kwh": initial_energy_kwh,
                                             "deadline": fmt_dt(deadline_dt),
                                             "departure_at": fmt_dt(departure_dt),
                                             "road_version": road_version,
                                             "priority": trip_priority,
                                             "driver_actor_id": driver_actor_id},
                                    create=create)

    # ------------------------------------------------------------------ 计划生成

    def plan_energy(self, *, request_id: str, actor_id: str, trip_id: str,
                    valid_minutes: int = PLAN_VALID_MINUTES) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            trip = self._trip(conn, trip_id)
            self._require_trip_actor(conn, actor, trip)
            if trip["state"] in ("completed", "abandoned"):
                raise ConflictError("任务已结束，不能再生成补能计划")
            active = conn.execute(
                "SELECT 1 FROM reservations WHERE trip_id=? AND status='active'",
                (trip_id,),
            ).fetchone()
            if active is not None:
                raise ConflictError("任务已有生效中的预约；如需调整请通过故障、排队超时、"
                                    "提前到达或道路封闭事件触发重规划")
            plan = self._create_plan(conn, actor_id=actor_id, trip=trip,
                                     reason="plan_created", valid_minutes=valid_minutes,
                                     event_kind="plan_replanned")
            return {"request_id": request_id, **self._plan_detail(conn, plan["plan_id"])}

    def _create_plan(self, conn, *, actor_id: str, trip, reason: str, valid_minutes: int,
                     event_kind: str, anchor_km: float | None = None,
                     anchor_at: datetime | None = None,
                     anchor_energy: float | None = None,
                     road_version: int | None = None,
                     event_detail: dict[str, Any] | None = None,
                     extra_occupancy: dict[tuple[str, datetime], dict] | None = None) -> dict[str, Any]:
        vehicle_row = self._vehicle(conn, trip["vehicle_id"])
        corridor = self._corridor(conn, trip["corridor_id"])
        version = road_version or conn.execute(
            "SELECT MAX(version) AS v FROM road_versions WHERE corridor_id=?",
            (trip["corridor_id"],),
        ).fetchone()["v"]
        if version is None:
            raise ValidationError("干线尚未发布道路通行版本")
        network = self._snapshot(conn, corridor_id=trip["corridor_id"], road_version=version,
                                 extra_occupancy=extra_occupancy)
        vehicle = charging.VehicleSpec(
            battery_kwh=vehicle_row["battery_kwh"],
            kwh_per_km_empty=vehicle_row["kwh_per_km_empty"],
            kwh_per_km_loaded=vehicle_row["kwh_per_km_loaded"],
            rated_payload_tonnes=vehicle_row["rated_payload_tonnes"],
            reserve_kwh=vehicle_row["reserve_kwh"],
        )
        if anchor_km is None:
            anchor_km = trip["origin_km"]
            anchor_at = parse_dt(trip["departure_at"])
            anchor_energy = trip["initial_energy_kwh"]
        assert anchor_at is not None and anchor_energy is not None
        outcome = charging.plan_trip(
            vehicle=vehicle, load_tonnes=trip["load_tonnes"], network=network,
            anchor_km=anchor_km, anchor_at=anchor_at, anchor_energy_kwh=anchor_energy,
            destination_km=trip["destination_km"], deadline=parse_dt(trip["deadline"]),
        )
        next_no = (conn.execute("SELECT COALESCE(MAX(plan_no),0)+1 AS n FROM energy_plans "
                                "WHERE trip_id=?", (trip["trip_id"],)).fetchone()["n"])
        plan_id = uuid.uuid4().hex
        now = self._now()
        valid_until = now + timedelta(minutes=valid_minutes)
        if outcome.legs:
            first_slot = outcome.legs[0].slot_start
            if first_slot < valid_until:
                valid_until = first_slot
        serial = outcome.to_serializable()
        summary = {"reason": reason, **serial}
        conn.execute(
            "UPDATE energy_plans SET status='superseded' WHERE trip_id=? AND status='proposed'",
            (trip["trip_id"],),
        )
        conn.execute(
            "INSERT INTO energy_plans(plan_id,trip_id,plan_no,road_version,status,valid_from,"
            "valid_until,anchor_km,anchor_at,anchor_energy_kwh,feasible,supersede_reason,"
            "summary_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, trip["trip_id"], next_no, version,
             "proposed", fmt_dt(now), fmt_dt(valid_until), anchor_km, fmt_dt(anchor_at),
             anchor_energy, 1 if outcome.feasible else 0, reason, canonical_json(summary),
             fmt_dt(now)),
        )
        for leg in outcome.legs:
            conn.execute(
                "INSERT INTO energy_plan_legs(plan_id,seq,station_id,arrive_at,slot_start,slot_end,"
                "power_kw,charge_kwh,arrive_energy_kwh,leave_energy_kwh) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (plan_id, leg.seq, leg.station_id, fmt_dt(leg.arrive_at), fmt_dt(leg.slot_start),
                 fmt_dt(leg.slot_end), leg.power_kw, leg.charge_kwh, leg.arrive_energy_kwh,
                 leg.leave_energy_kwh),
            )
        event_id = uuid.uuid4().hex
        detail = {"reason": reason, "trigger": reason, "feasible": outcome.feasible,
                  "to_plan_id": plan_id, "plan_no": next_no, "road_version": version,
                  "reasons": list(outcome.reasons), **(event_detail or {})}
        conn.execute(
            "INSERT INTO trip_events(event_id,trip_id,kind,at_km,from_plan_id,to_plan_id,"
            "detail_json,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, trip["trip_id"], event_kind, anchor_km, None, plan_id,
             canonical_json(detail), fmt_dt(self._now()), fmt_dt(now)),
        )
        self._audit(conn, actor_id=actor_id, action=f"plan.{event_kind}",
                    resource_type="energy_plan", resource_id=plan_id,
                    detail={"trip_id": trip["trip_id"], "plan_no": next_no,
                            "feasible": outcome.feasible, "reason": reason})
        return {"plan_id": plan_id, "plan_no": next_no}

    # ------------------------------------------------------------------ 确认与锁定

    def confirm_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            plan = conn.execute("SELECT * FROM energy_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("补能计划不存在")
            trip = self._trip(conn, plan["trip_id"])
            self._require_trip_actor(conn, actor, trip)

            def create() -> tuple[str, str, dict[str, Any]]:
                if plan["status"] != "proposed":
                    raise ConflictError(f"计划当前状态为 {plan['status']}，不能确认")
                now = self._now()
                if now > parse_dt(plan["valid_until"]):
                    conn.execute("UPDATE energy_plans SET status='expired' WHERE plan_id=?",
                                 (plan_id,))
                    raise ConflictError("补能计划已超过有效期，请重新获取")
                legs = conn.execute(
                    "SELECT * FROM energy_plan_legs WHERE plan_id=? ORDER BY seq", (plan_id,)
                ).fetchall()
                if not legs:
                    raise ConflictError("不可行的补能计划不能确认")
                if parse_dt(legs[0]["slot_start"]) < now:
                    raise ConflictError("首个时隙已经开始，计划不能再锁定，请重新规划")

                old_reservation = conn.execute(
                    "SELECT * FROM reservations WHERE trip_id=? AND status='active'",
                    (trip["trip_id"],),
                ).fetchone()
                if old_reservation is not None:
                    raise ConflictError("该任务已有生效中的预约，请先处理")

                requested: list[dict[str, Any]] = []
                plan_summary = json.loads(plan["summary_json"])
                draw_by_seq = {
                    item["seq"]: {parse_dt(slot): draw for slot, draw in item.get("draws", [])}
                    for item in plan_summary["legs"]
                }
                for leg in legs:
                    station = self._station(conn, leg["station_id"])
                    charger = conn.execute(
                        "SELECT * FROM chargers WHERE charger_id=? AND station_id=?",
                        (self._planned_charger(conn, plan, leg), leg["station_id"]),
                    ).fetchone()
                    if charger is None:
                        raise ConflictError(f"站点 {leg['station_id']} 的规划充电桩不可用")
                    draw_map = draw_by_seq.get(leg["seq"])
                    if not draw_map:
                        raise ConflictError("计划缺少逐时隙功率数据")
                    requested.append({"leg": leg, "station": station, "charger": charger,
                                      "draw_map": draw_map})

                reservation_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO reservations(reservation_id,plan_id,trip_id,status,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (reservation_id, plan_id, trip["trip_id"], "active", self._now_text()),
                )
                if trip["priority"] == "rescue":
                    self._displace_for_rescue(conn, actor_id=actor_id, trip=trip,
                                              requested=requested,
                                              ignore_reservation_id=reservation_id)

                for item in requested:
                    self._lock_slots(conn, reservation_id=reservation_id, item=item,
                                     plan=plan)
                for item in requested:
                    leg = item["leg"]
                    conn.execute(
                        "INSERT INTO reservation_legs(reservation_id,seq,station_id,slot_start,"
                        "slot_end,charger_id,power_kw,charge_kwh,leg_state) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (reservation_id, leg["seq"], leg["station_id"], leg["slot_start"],
                         leg["slot_end"], item["charger"]["charger_id"], leg["power_kw"],
                         leg["charge_kwh"], "reserved"),
                    )
                conn.execute("UPDATE energy_plans SET status='confirmed' WHERE plan_id=?", (plan_id,))
                conn.execute("UPDATE trips SET state='en_route' WHERE trip_id=? AND state='planned'",
                             (trip["trip_id"],))
                event_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO trip_events(event_id,trip_id,kind,at_km,from_plan_id,to_plan_id,"
                    "detail_json,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (event_id, trip["trip_id"], "plan_confirmed", None, plan_id, plan_id,
                     canonical_json({"reservation_id": reservation_id,
                                     "legs": len(requested), "priority": trip["priority"]}),
                     self._now_text(), self._now_text()),
                )
                self._audit(conn, actor_id=actor_id, action="plan.confirmed",
                            resource_type="reservation", resource_id=reservation_id,
                            detail={"trip_id": trip["trip_id"], "plan_id": plan_id,
                                    "legs": len(requested)})
                return "reservation", reservation_id, {
                    "reservation_id": reservation_id, "plan_id": plan_id,
                    "trip_id": trip["trip_id"], "legs": len(requested),
                    "locked_slots": sum(len(item["draw_map"]) for item in requested),
                }

            return self._idempotent(conn, request_id=request_id, action="confirm_plan",
                                    payload={"actor_id": actor_id, "plan_id": plan_id},
                                    create=create)

    def _planned_charger(self, conn, plan, leg) -> str:
        """从计划摘要中取回规划器选定的充电桩。"""

        summary = json.loads(plan["summary_json"])
        for item in summary["legs"]:
            if item["seq"] == leg["seq"]:
                return item["charger_id"]
        raise ConflictError("计划缺少充电桩分配")

    def _lock_slots(self, conn, *, reservation_id: str, item: dict[str, Any], plan) -> None:
        leg = item["leg"]
        station = item["station"]
        charger = item["charger"]
        for slot, power in item["draw_map"].items():
            self._assert_charger_usable(conn, station=station, charger=charger, slot=slot,
                                        power_kw=power, ignore_reservation_id=reservation_id)
        for slot, power in item["draw_map"].items():
            conn.execute(
                "INSERT INTO slot_occupancy(station_id,slot_start,reservation_id,seq,charger_id,"
                "draw_kw,occupancy_state) VALUES(?,?,?,?,?,?,?)",
                (station["station_id"], fmt_dt(slot), reservation_id, leg["seq"],
                 charger["charger_id"], power, "reserved"),
            )

    def _assert_charger_usable(self, conn, *, station, charger, slot: datetime, power_kw: float,
                               ignore_reservation_id: str) -> None:
        if not station["active"]:
            raise ConflictError(f"站点 {station['station_id']} 已停用")
        if not charger["active"]:
            raise ConflictError(f"充电桩 {charger['charger_id']} 已停用")
        if self._in_outage(conn, "station_outages", "station_id", station["station_id"], slot):
            raise ConflictError(f"站点 {station['station_id']} 在 {fmt_dt(slot)} 处于检修")
        if self._in_outage(conn, "charger_outages", "charger_id", charger["charger_id"], slot):
            raise ConflictError(f"充电桩 {charger['charger_id']} 在 {fmt_dt(slot)} 处于检修")
        physical = conn.execute(
            "SELECT reservation_id FROM slot_occupancy WHERE station_id=? AND slot_start=? "
            "AND charger_id=?",
            (station["station_id"], fmt_dt(slot), charger["charger_id"]),
        ).fetchone()
        if physical is not None and physical["reservation_id"] != ignore_reservation_id:
            raise ConflictError(f"充电桩 {charger['charger_id']} 在 {fmt_dt(slot)} 已被占用")
        cap = self._station_cap_at(conn, station=station, slot=slot)
        used = conn.execute(
            "SELECT COALESCE(SUM(draw_kw),0) AS used FROM slot_occupancy WHERE station_id=? "
            "AND slot_start=? AND reservation_id!=?",
            (station["station_id"], fmt_dt(slot), ignore_reservation_id),
        ).fetchone()["used"]
        if used + power_kw > cap + 1e-6:
            raise ConflictError(
                f"站点 {station['station_id']} 在 {fmt_dt(slot)} 功率限额 {cap:.1f}kW，"
                f"已占用 {used:.1f}kW，无法再承载 {power_kw:.1f}kW"
            )

    def _station_cap_at(self, conn, *, station, slot: datetime) -> float:
        """返回站点某时隙的总功率上限：分时限额与在线桩总额取较小值。"""

        rated_total = conn.execute(
            "SELECT COALESCE(SUM(c.rated_power_kw),0) AS total FROM chargers c WHERE c.station_id=? "
            "AND c.active=1 AND NOT EXISTS(SELECT 1 FROM charger_outages o WHERE o.charger_id="
            "c.charger_id AND ?>=o.starts_at AND ?<o.ends_at)",
            (station["station_id"], fmt_dt(slot), fmt_dt(slot)),
        ).fetchone()["total"]
        cap_row = conn.execute(
            "SELECT MIN(cap_kw) AS cap FROM power_windows WHERE station_id=? AND ?>=starts_at "
            "AND ?<ends_at",
            (station["station_id"], fmt_dt(slot), fmt_dt(slot)),
        ).fetchone()["cap"]
        if cap_row is None:
            return rated_total
        return min(rated_total, cap_row)

    def _in_outage(self, conn, table: str, column: str, value: str, slot: datetime) -> bool:
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE {column}=? AND ?>=starts_at AND ?<ends_at LIMIT 1",
            (value, fmt_dt(slot), fmt_dt(slot)),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ 抢险挤占

    def _displace_for_rescue(self, conn, *, actor_id: str, trip, requested: list[dict],
                             ignore_reservation_id: str) -> None:
        """抢险任务锁定前，挤掉冲突时隙上尚未开始充电的普通预约。

        已经开始（存在充电会话）的预约受物理保护，此时直接失败，整体回滚，
        因此不会留下任何半占用状态。被挤车辆重规划时，抢险任务即将锁定的
        时隙以虚拟占用形式计入，避免新计划再次撞上抢险时隙。
        """

        rescue_occupancy: dict[tuple[str, datetime], dict] = {}
        for item in requested:
            for slot, power in item["draw_map"].items():
                key = (item["station"]["station_id"], slot)
                bucket = rescue_occupancy.setdefault(key, {"used_kw": 0.0, "busy": {}})
                bucket["used_kw"] += power
                bucket["busy"][item["charger"]["charger_id"]] = power

        displaced: set[str] = set()
        for _ in range(256):
            conflict = self._find_conflict(conn, requested,
                                           ignore_reservation_id=ignore_reservation_id)
            if conflict is None:
                return
            other_reservation_id, station_id, slot = conflict
            if other_reservation_id in displaced:
                raise ConflictError("抢险抢占出现无法解除的冲突")
            other = conn.execute("SELECT * FROM reservations WHERE reservation_id=?",
                                 (other_reservation_id,)).fetchone()
            other_trip = self._trip(conn, other["trip_id"])
            # 任何已经开始充电会话的预约都受物理保护，抢险优先权不能挤掉
            started = conn.execute(
                "SELECT 1 FROM charging_sessions WHERE reservation_id=? LIMIT 1",
                (other_reservation_id,),
            ).fetchone()
            if started is not None:
                raise ConflictError(
                    f"任务 {other_trip['trip_id']} 在站点 {station_id} 已开始充电会话，"
                    "抢险优先权不能挤掉"
                )
            if other_trip["priority"] == "rescue":
                raise ConflictError("抢险任务之间不能互相挤占")
            # 尚未产生任何充电事实：释放该普通预约的全部未来时隙
            conn.execute(
                "UPDATE reservation_legs SET leg_state='released' WHERE reservation_id=? "
                "AND leg_state IN ('reserved','arrived')",
                (other_reservation_id,),
            )
            conn.execute("DELETE FROM slot_occupancy WHERE reservation_id=?", (other_reservation_id,))
            conn.execute("UPDATE reservations SET status='displaced_by_rescue' WHERE reservation_id=?",
                         (other_reservation_id,))
            conn.execute("UPDATE energy_plans SET status='superseded', "
                         "supersede_reason='displaced_by_rescue' WHERE plan_id=?",
                         (other["plan_id"],))
            self._replan_after_event(conn, actor_id="rescue-policy", trip=other_trip,
                                     kind="rescue_displaced",
                                     occurred_at=self._now(),
                                     anchor_km=other_trip["origin_km"],
                                     anchor_energy=other_trip["initial_energy_kwh"],
                                     detail={"by_trip_id": trip["trip_id"],
                                             "station_id": station_id,
                                             "slot": fmt_dt(slot),
                                             "from_plan_id": other["plan_id"]},
                                     from_plan_id=other["plan_id"],
                                     valid_minutes=REPLAN_VALID_MINUTES,
                                     extra_occupancy=rescue_occupancy)
            displaced.add(other_reservation_id)
        raise ConflictError("抢险抢占级联超出上限")

    def _find_conflict(self, conn, requested: list[dict], ignore_reservation_id: str = ""):
        for item in requested:
            station = item["station"]
            charger = item["charger"]
            for slot, need_kw in item["draw_map"].items():
                physical = conn.execute(
                    "SELECT reservation_id FROM slot_occupancy WHERE station_id=? AND slot_start=? "
                    "AND charger_id=? AND reservation_id!=?",
                    (station["station_id"], fmt_dt(slot), charger["charger_id"],
                     ignore_reservation_id),
                ).fetchone()
                if physical is not None:
                    return physical["reservation_id"], station["station_id"], slot
                cap = self._station_cap_at(conn, station=station, slot=slot)
                used_row = conn.execute(
                    "SELECT reservation_id, COALESCE(SUM(draw_kw),0) AS used FROM slot_occupancy "
                    "WHERE station_id=? AND slot_start=? AND reservation_id!=? "
                    "GROUP BY reservation_id ORDER BY used DESC",
                    (station["station_id"], fmt_dt(slot), ignore_reservation_id),
                ).fetchall()
                used = sum(row["used"] for row in used_row)
                if used + need_kw > cap + 1e-6:
                    return used_row[0]["reservation_id"], station["station_id"], slot
        return None

    # ------------------------------------------------------------------ 到站与会话

    def mark_arrival(self, *, request_id: str, actor_id: str, trip_id: str, station_id: str,
                     occurred_at: str | None = None) -> dict[str, Any]:
        moment = parse_dt(occurred_at) if occurred_at else self._now()
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            trip = self._trip(conn, trip_id)
            self._require_trip_actor(conn, actor, trip)

            def create() -> tuple[str, str, dict[str, Any]]:
                reservation = self._active_reservation(conn, trip_id)
                leg = self._reservation_leg(conn, reservation, station_id,
                                            states=("reserved",))
                planned_start = parse_dt(leg["slot_start"])
                if moment < planned_start - timedelta(minutes=ARRIVAL_TOLERANCE_MINUTES):
                    raise ConflictError("到站时间明显早于预约时隙，请按提前到达流程重新规划")
                conn.execute(
                    "UPDATE reservation_legs SET leg_state='arrived', actual_arrival=? "
                    "WHERE reservation_id=? AND seq=?",
                    (fmt_dt(moment), reservation["reservation_id"], leg["seq"]),
                )
                conn.execute(
                    "UPDATE slot_occupancy SET occupancy_state='arrived' WHERE reservation_id=? "
                    "AND seq=? AND station_id=?",
                    (reservation["reservation_id"], leg["seq"], station_id),
                )
                self._record_event(conn, trip=trip, kind="arrival", at_km=None,
                                   occurred_at=moment,
                                   detail={"station_id": station_id,
                                           "reservation_id": reservation["reservation_id"]})
                self._audit(conn, actor_id=actor_id, action="trip.arrived",
                            resource_type="trip", resource_id=trip_id,
                            detail={"station_id": station_id})
                return "trip_event", f"{trip_id}:{station_id}", {
                    "trip_id": trip_id, "station_id": station_id, "state": "arrived"}

            return self._idempotent(conn, request_id=request_id, action="mark_arrival",
                                    payload={"actor_id": actor_id, "trip_id": trip_id,
                                             "station_id": station_id,
                                             "occurred_at": fmt_dt(moment)}, create=create)

    def start_session(self, *, request_id: str, actor_id: str, trip_id: str, station_id: str,
                      started_at: str | None = None) -> dict[str, Any]:
        moment = parse_dt(started_at) if started_at else self._now()
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            trip = self._trip(conn, trip_id)
            self._require_trip_actor(conn, actor, trip)

            def create() -> tuple[str, str, dict[str, Any]]:
                reservation = self._active_reservation(conn, trip_id)
                leg = self._reservation_leg(conn, reservation, station_id,
                                            states=("arrived", "reserved"))
                session_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO charging_sessions(session_id,reservation_id,seq,trip_id,"
                    "station_id,charger_id,started_at,session_state) VALUES(?,?,?,?,?,?,?,?)",
                    (session_id, reservation["reservation_id"], leg["seq"], trip_id, station_id,
                     leg["charger_id"], fmt_dt(moment), "charging"),
                )
                conn.execute(
                    "UPDATE reservation_legs SET leg_state='charging', started_at=? "
                    "WHERE reservation_id=? AND seq=?",
                    (fmt_dt(moment), reservation["reservation_id"], leg["seq"]),
                )
                conn.execute(
                    "UPDATE slot_occupancy SET occupancy_state='charging' WHERE reservation_id=? "
                    "AND seq=? AND station_id=?",
                    (reservation["reservation_id"], leg["seq"], station_id),
                )
                self._record_event(conn, trip=trip, kind="session_started", at_km=None,
                                   occurred_at=moment,
                                   detail={"station_id": station_id, "session_id": session_id,
                                           "charger_id": leg["charger_id"]})
                self._audit(conn, actor_id=actor_id, action="session.started",
                            resource_type="charging_session", resource_id=session_id,
                            detail={"trip_id": trip_id, "station_id": station_id})
                return "charging_session", session_id, {"session_id": session_id,
                                                         "station_id": station_id,
                                                         "state": "charging"}

            return self._idempotent(conn, request_id=request_id, action="start_session",
                                    payload={"actor_id": actor_id, "trip_id": trip_id,
                                             "station_id": station_id,
                                             "started_at": fmt_dt(moment)}, create=create)

    def complete_session(self, *, request_id: str, actor_id: str, trip_id: str,
                         station_id: str, delivered_kwh: float,
                         ended_at: str | None = None) -> dict[str, Any]:
        moment = parse_dt(ended_at) if ended_at else self._now()
        if delivered_kwh < 0:
            raise ValidationError("delivered_kwh 不能为负")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            trip = self._trip(conn, trip_id)
            self._require_trip_actor(conn, actor, trip)

            def create() -> tuple[str, str, dict[str, Any]]:
                reservation = self._active_reservation(conn, trip_id)
                leg = self._reservation_leg(conn, reservation, station_id,
                                            states=("charging",))
                session = conn.execute(
                    "SELECT * FROM charging_sessions WHERE reservation_id=? AND seq=? "
                    "AND session_state='charging'",
                    (reservation["reservation_id"], leg["seq"]),
                ).fetchone()
                conn.execute(
                    "UPDATE charging_sessions SET session_state='completed', ended_at=?, "
                    "delivered_kwh=? WHERE session_id=?",
                    (fmt_dt(moment), delivered_kwh, session["session_id"]),
                )
                conn.execute(
                    "UPDATE reservation_legs SET leg_state='completed', completed_at=?, "
                    "delivered_kwh=? WHERE reservation_id=? AND seq=?",
                    (fmt_dt(moment), delivered_kwh, reservation["reservation_id"], leg["seq"]),
                )
                # 充电结束后释放尚未使用的时隙；时隙中途结束时保留当前时隙，
                # 避免同一物理时隙被重复分配
                conn.execute(
                    "DELETE FROM slot_occupancy WHERE reservation_id=? AND seq=? AND slot_start>=?",
                    (reservation["reservation_id"], leg["seq"],
                     fmt_dt(charging.ceil_slot(moment))),
                )
                self._record_event(conn, trip=trip, kind="session_completed", at_km=None,
                                   occurred_at=moment,
                                   detail={"station_id": station_id,
                                           "delivered_kwh": delivered_kwh,
                                           "session_id": session["session_id"]})
                self._audit(conn, actor_id=actor_id, action="session.completed",
                            resource_type="charging_session",
                            resource_id=session["session_id"],
                            detail={"trip_id": trip_id, "station_id": station_id,
                                    "delivered_kwh": delivered_kwh})
                return "charging_session", session["session_id"], {
                    "session_id": session["session_id"], "station_id": station_id,
                    "state": "completed", "delivered_kwh": delivered_kwh}

            return self._idempotent(conn, request_id=request_id, action="complete_session",
                                    payload={"actor_id": actor_id, "trip_id": trip_id,
                                             "station_id": station_id,
                                             "delivered_kwh": delivered_kwh,
                                             "ended_at": fmt_dt(moment)}, create=create)

    # ------------------------------------------------------------------ 事件与重规划

    def report_event(self, *, request_id: str, actor_id: str, trip_id: str, kind: str,
                     at_km: float | None = None, occurred_at: str | None = None,
                     detail: dict[str, Any] | None = None) -> dict[str, Any]:
        if kind not in ("fault", "queue_timeout", "early_arrival", "road_closure"):
            raise ValidationError("不支持的事件类型")
        moment = parse_dt(occurred_at) if occurred_at else self._now()
        detail = detail or {}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            trip = self._trip(conn, trip_id)
            self._require_trip_actor(conn, actor, trip)

            def create() -> tuple[str, str, dict[str, Any]]:
                reservation_row = conn.execute(
                    "SELECT * FROM reservations WHERE trip_id=? AND status='active'",
                    (trip_id,),
                ).fetchone()
                previous_plan_id = reservation_row["plan_id"] if reservation_row else None
                station_id = detail.get("station_id")
                if reservation_row is not None:
                    self._release_for_event(conn, reservation=reservation_row, kind=kind,
                                            moment=moment, station_id=station_id,
                                            delivered_kwh=detail.get("delivered_kwh"))
                anchor_km, anchor_energy = self._anchor_from_facts(
                    conn, trip=trip, at_km=at_km,
                    station_km=self._station_km(conn, station_id) if station_id else None,
                )
                new_version = None
                if kind == "road_closure":
                    new_version = conn.execute(
                        "SELECT MAX(version) AS v FROM road_versions WHERE corridor_id=?",
                        (trip["corridor_id"],),
                    ).fetchone()["v"]
                    wanted = detail.get("road_version")
                    if wanted is not None and wanted != new_version:
                        raise ConflictError("指定道路版本与最新发布版本不一致")
                self._record_event(conn, trip=trip, kind=kind, at_km=anchor_km,
                                   occurred_at=moment,
                                   detail={"trigger": kind, "from_plan_id": previous_plan_id,
                                           **detail})
                self._replan_after_event(conn, actor_id=actor_id, trip=trip, kind=kind,
                                         occurred_at=moment, anchor_km=anchor_km,
                                         anchor_energy=anchor_energy, detail=detail,
                                         road_version=new_version,
                                         from_plan_id=previous_plan_id,
                                         valid_minutes=REPLAN_VALID_MINUTES)
                new_plan = conn.execute(
                    "SELECT * FROM energy_plans WHERE trip_id=? ORDER BY plan_no DESC LIMIT 1",
                    (trip_id,),
                ).fetchone()
                return "energy_plan", new_plan["plan_id"], {
                    "trip_id": trip_id, "kind": kind,
                    "plan_id": new_plan["plan_id"], "plan_no": new_plan["plan_no"],
                    "feasible": bool(new_plan["feasible"]),
                    "valid_until": new_plan["valid_until"]}

            return self._idempotent(conn, request_id=request_id, action=f"event:{kind}",
                                    payload={"actor_id": actor_id, "trip_id": trip_id,
                                             "kind": kind, "at_km": at_km,
                                             "occurred_at": fmt_dt(moment), "detail": detail},
                                    create=create)

    def _release_for_event(self, conn, *, reservation, kind: str, moment: datetime,
                           station_id: str | None, delivered_kwh: float | None) -> None:
        """按事件释放未开始的占用；故障时结算当前会话的实充事实。"""

        legs = conn.execute("SELECT * FROM reservation_legs WHERE reservation_id=? ORDER BY seq",
                            (reservation["reservation_id"],)).fetchall()
        for leg in legs:
            if leg["leg_state"] in ("completed",):
                continue
            if leg["leg_state"] == "charging":
                if kind == "fault" and leg["station_id"] == station_id:
                    session = conn.execute(
                        "SELECT * FROM charging_sessions WHERE reservation_id=? AND seq=? "
                        "AND session_state='charging'",
                        (reservation["reservation_id"], leg["seq"]),
                    ).fetchone()
                    delivered = float(delivered_kwh) if delivered_kwh is not None else 0.0
                    conn.execute(
                        "UPDATE charging_sessions SET session_state='faulted', ended_at=?, "
                        "delivered_kwh=? WHERE session_id=?",
                        (fmt_dt(moment), delivered, session["session_id"]),
                    )
                    conn.execute(
                        "UPDATE reservation_legs SET leg_state='faulted', completed_at=?, "
                        "delivered_kwh=? WHERE reservation_id=? AND seq=?",
                        (fmt_dt(moment), delivered, reservation["reservation_id"], leg["seq"]),
                    )
                else:
                    # 道路封闭等事件发生时，正在进行的会话视为按计划完成到当前事实
                    session = conn.execute(
                        "SELECT * FROM charging_sessions WHERE reservation_id=? AND seq=? "
                        "AND session_state='charging'",
                        (reservation["reservation_id"], leg["seq"]),
                    ).fetchone()
                    delivered = float(delivered_kwh) if delivered_kwh is not None else 0.0
                    conn.execute(
                        "UPDATE charging_sessions SET session_state='completed', ended_at=?, "
                        "delivered_kwh=? WHERE session_id=?",
                        (fmt_dt(moment), delivered, session["session_id"]),
                    )
                    conn.execute(
                        "UPDATE reservation_legs SET leg_state='completed', completed_at=?, "
                        "delivered_kwh=? WHERE reservation_id=? AND seq=?",
                        (fmt_dt(moment), delivered, reservation["reservation_id"], leg["seq"]),
                    )
                continue
            conn.execute(
                "UPDATE reservation_legs SET leg_state='released' WHERE reservation_id=? AND seq=? "
                "AND leg_state IN ('reserved','arrived')",
                (reservation["reservation_id"], leg["seq"]),
            )
        # 整个预约即将作废：删除其全部时隙占用（充电事实由会话表与预约腿表保留）
        conn.execute(
            "DELETE FROM slot_occupancy WHERE reservation_id=?",
            (reservation["reservation_id"],),
        )
        conn.execute("UPDATE reservations SET status='superseded' WHERE reservation_id=?",
                     (reservation["reservation_id"],))
        conn.execute(
            "UPDATE energy_plans SET status='superseded', supersede_reason=? WHERE plan_id=?",
            (kind, reservation["plan_id"]),
        )

    def _anchor_from_facts(self, conn, *, trip, at_km: float | None,
                           station_km: float | None) -> tuple[float, float]:
        """以已完成充电会话为事实，结合当前道路版本里程，推导能量锚点。"""

        vehicle = self._vehicle(conn, trip["vehicle_id"])
        version = conn.execute(
            "SELECT MAX(version) AS v FROM road_versions WHERE corridor_id=?",
            (trip["corridor_id"],),
        ).fetchone()["v"]
        segments = self._road_segments(conn, trip["corridor_id"], version)
        network = charging.NetworkSnapshot(stations=[], segments=segments,
                                           avg_speed_kmh=1.0)
        consumption = network.consumption(
            charging.VehicleSpec(vehicle["battery_kwh"], vehicle["kwh_per_km_empty"],
                                 vehicle["kwh_per_km_loaded"], vehicle["rated_payload_tonnes"],
                                 vehicle["reserve_kwh"]),
            trip["load_tonnes"],
        )
        delivered = conn.execute(
            "SELECT COALESCE(SUM(delivered_kwh),0) AS total FROM charging_sessions s "
            "JOIN reservations r ON r.reservation_id=s.reservation_id WHERE r.trip_id=? "
            "AND s.session_state IN ('completed','faulted')",
            (trip["trip_id"],),
        ).fetchone()["total"]
        anchor_km = at_km if at_km is not None else (station_km if station_km is not None
                                                      else trip["origin_km"])
        driven = network.path_distance(trip["origin_km"], anchor_km)
        energy = trip["initial_energy_kwh"] - driven * consumption + delivered
        return anchor_km, energy

    def _replan_after_event(self, conn, *, actor_id: str, trip, kind: str, occurred_at: datetime,
                            detail: dict[str, Any], anchor_km: float | None = None,
                            anchor_energy: float | None = None, road_version: int | None = None,
                            valid_minutes: int = REPLAN_VALID_MINUTES,
                            from_plan_id: str | None = None,
                            extra_occupancy: dict[tuple[str, datetime], dict] | None = None
                            ) -> dict[str, Any]:
        created = self._create_plan(
            conn, actor_id=actor_id, trip=trip, reason=kind, valid_minutes=valid_minutes,
            event_kind="plan_replanned", anchor_km=anchor_km, anchor_at=occurred_at,
            anchor_energy=anchor_energy, road_version=road_version,
            event_detail={"trigger": kind, **(detail or {})},
            extra_occupancy=extra_occupancy,
        )
        if from_plan_id:
            conn.execute(
                "UPDATE trip_events SET from_plan_id=? WHERE trip_id=? AND to_plan_id=? "
                "AND from_plan_id IS NULL",
                (from_plan_id, trip["trip_id"], created["plan_id"]),
            )
        return created

    def _record_event(self, conn, *, trip, kind: str, at_km: float | None,
                      occurred_at: datetime, detail: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO trip_events(event_id,trip_id,kind,at_km,from_plan_id,to_plan_id,"
            "detail_json,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, trip["trip_id"], kind, at_km, None, None,
             canonical_json(detail), fmt_dt(occurred_at), self._now_text()),
        )

    # ------------------------------------------------------------------ 运营查询

    def trip_safety(self, trip_id: str) -> dict[str, Any]:
        with self.database.transaction() as conn:
            trip = self._trip(conn, trip_id)
            vehicle_row = self._vehicle(conn, trip["vehicle_id"])
            version = conn.execute(
                "SELECT MAX(version) AS v FROM road_versions WHERE corridor_id=?",
                (trip["corridor_id"],),
            ).fetchone()["v"]
            network = self._snapshot(conn, corridor_id=trip["corridor_id"], road_version=version)
            vehicle = charging.VehicleSpec(
                battery_kwh=vehicle_row["battery_kwh"],
                kwh_per_km_empty=vehicle_row["kwh_per_km_empty"],
                kwh_per_km_loaded=vehicle_row["kwh_per_km_loaded"],
                rated_payload_tonnes=vehicle_row["rated_payload_tonnes"],
                reserve_kwh=vehicle_row["reserve_kwh"],
            )
            anchor_km, anchor_at, anchor_energy, anchor_basis = self._current_anchor(
                conn, trip=trip, network=network, vehicle=vehicle)
            reach = charging.evaluate_reach(
                vehicle=vehicle, load_tonnes=trip["load_tonnes"], network=network,
                anchor_km=anchor_km, anchor_at=anchor_at, anchor_energy_kwh=anchor_energy,
                destination_km=trip["destination_km"],
            )
            consumption = network.consumption(vehicle, trip["load_tonnes"])
            active = conn.execute(
                "SELECT * FROM reservations WHERE trip_id=? AND status='active'", (trip_id,)
            ).fetchone()
            plan_path = None
            if active is not None:
                plan_path = self._plan_path(conn, reservation=active, network=network,
                                            consumption=consumption, anchor_km=anchor_km,
                                            anchor_energy=anchor_energy,
                                            deadline=parse_dt(trip["deadline"]))
                safe = plan_path["destination"]["safely_reachable"]
            else:
                safe = reach["destination"]["safely_reachable"]
            result = {
                "trip_id": trip_id,
                "state": trip["state"],
                "priority": trip["priority"],
                "road_version": version,
                "anchor_km": anchor_km,
                "anchor_at": fmt_dt(anchor_at),
                "anchor_energy_kwh": round(anchor_energy, 3),
                "anchor_basis": anchor_basis,
                "safely_reachable": safe,
                "deadline": trip["deadline"],
                "reach": reach,
            }
            if plan_path is not None:
                result["plan_path"] = plan_path
            if active is not None:
                result["reservation"] = {
                    "reservation_id": active["reservation_id"],
                    "plan_id": active["plan_id"],
                    "legs": [
                        dict(row) for row in conn.execute(
                            "SELECT seq,station_id,slot_start,slot_end,charger_id,power_kw,"
                            "charge_kwh,leg_state,actual_arrival,started_at,completed_at,"
                            "delivered_kwh FROM reservation_legs WHERE reservation_id=? ORDER BY seq",
                            (active["reservation_id"],)).fetchall()
                    ],
                }
            return result

    def _plan_path(self, conn, *, reservation, network: charging.NetworkSnapshot,
                   consumption: float, anchor_km: float, anchor_energy: float,
                   deadline: datetime) -> dict[str, Any]:
        """按当前锚点与生效中预约的计划补能，复算沿途与终点的安全余量。"""

        vehicle_row = self._vehicle(conn, conn.execute(
            "SELECT vehicle_id FROM trips WHERE trip_id=?",
            (reservation["trip_id"],)).fetchone()["vehicle_id"])
        battery = vehicle_row["battery_kwh"]
        reserve = vehicle_row["reserve_kwh"]
        energy = anchor_energy
        pos = anchor_km
        legs_out = []
        future_states = ("reserved", "arrived", "charging")
        legs = conn.execute(
            "SELECT l.*, c.position_km FROM reservation_legs l "
            "JOIN charging_stations c ON c.station_id=l.station_id "
            "WHERE l.reservation_id=? AND l.leg_state IN (?,?,?) ORDER BY l.seq",
            (reservation["reservation_id"], *future_states),
        ).fetchall()
        feasible = True
        for leg in legs:
            dist = network.path_distance(pos, leg["position_km"])
            arrive_energy = energy - dist * consumption
            leave_energy = min(battery, arrive_energy + leg["charge_kwh"])
            margin = arrive_energy - reserve
            if margin < -1e-6:
                feasible = False
            legs_out.append({
                "seq": leg["seq"], "station_id": leg["station_id"],
                "position_km": leg["position_km"], "path_km": round(dist, 2),
                "arrive_energy_kwh": round(arrive_energy, 3),
                "margin_kwh": round(margin, 3),
                "margin_km": round(margin / consumption, 2),
                "charge_kwh": leg["charge_kwh"],
                "leg_state": leg["leg_state"],
            })
            energy = leave_energy
            pos = leg["position_km"]
        dest = conn.execute(
            "SELECT destination_km FROM trips WHERE trip_id=?", (reservation["trip_id"],)
        ).fetchone()["destination_km"]
        dist_dest = network.path_distance(pos, dest)
        dest_energy = energy - dist_dest * consumption
        dest_margin = dest_energy - reserve
        if dest_margin < -1e-6:
            feasible = False
        return {
            "feasible": feasible,
            "legs": legs_out,
            "destination": {
                "position_km": dest, "path_km": round(dist_dest, 2),
                "arrive_energy_kwh": round(dest_energy, 3),
                "margin_kwh": round(dest_margin, 3),
                "margin_km": round(dest_margin / consumption, 2),
                "safely_reachable": dest_margin >= -1e-6,
            },
        }

    def _current_anchor(self, conn, *, trip, network: charging.NetworkSnapshot,
                        vehicle: charging.VehicleSpec) -> tuple[float, datetime, float, str]:
        """依据事件位置与已完成充电事实推导车辆当前能量锚点。"""

        consumption = network.consumption(vehicle, trip["load_tonnes"])

        def energy_at(pos: float, delivered: float) -> float:
            driven = network.path_distance(trip["origin_km"], pos)
            return trip["initial_energy_kwh"] - driven * consumption + delivered

        delivered = conn.execute(
            "SELECT COALESCE(SUM(delivered_kwh),0) AS total FROM charging_sessions s "
            "JOIN reservations r ON r.reservation_id=s.reservation_id WHERE r.trip_id=? "
            "AND s.session_state IN ('completed','faulted')",
            (trip["trip_id"],),
        ).fetchone()["total"]

        last_position_event = conn.execute(
            "SELECT * FROM trip_events WHERE trip_id=? AND at_km IS NOT NULL "
            "ORDER BY occurred_at DESC, created_at DESC LIMIT 1",
            (trip["trip_id"],),
        ).fetchone()
        if last_position_event is not None:
            pos = last_position_event["at_km"]
            return (pos, parse_dt(last_position_event["occurred_at"]),
                    energy_at(pos, delivered), f"event:{last_position_event['kind']}")

        last_session = conn.execute(
            "SELECT s.*, c.position_km FROM charging_sessions s "
            "JOIN reservations r ON r.reservation_id=s.reservation_id "
            "JOIN charging_stations c ON c.station_id=s.station_id "
            "WHERE r.trip_id=? ORDER BY COALESCE(s.ended_at,s.started_at) DESC LIMIT 1",
            (trip["trip_id"],),
        ).fetchone()
        if last_session is not None and last_session["ended_at"]:
            return (last_session["position_km"], parse_dt(last_session["ended_at"]),
                    energy_at(last_session["position_km"], delivered),
                    f"session:{last_session['session_state']}")

        active = conn.execute(
            "SELECT r.*, l.seq AS leg_seq, l.leg_state, l.actual_arrival, l.started_at, "
            "c.position_km FROM reservations r "
            "JOIN reservation_legs l ON l.reservation_id=r.reservation_id "
            "JOIN charging_stations c ON c.station_id=l.station_id "
            "WHERE r.trip_id=? AND r.status='active' AND l.leg_state IN ('arrived','charging') "
            "ORDER BY l.seq DESC LIMIT 1",
            (trip["trip_id"],),
        ).fetchone()
        if active is not None:
            when = active["started_at"] or active["actual_arrival"]
            return (active["position_km"], parse_dt(when),
                    energy_at(active["position_km"], delivered),
                    f"leg:{active['leg_state']}")

        return (trip["origin_km"], parse_dt(trip["departure_at"]),
                trip["initial_energy_kwh"], "origin")

    def safety_board(self, corridor_id: str | None = None) -> dict[str, Any]:
        """调度看板：逐辆在途车辆是否仍可安全抵达，直接回答"哪些车还能到"。"""

        query = "SELECT trip_id FROM trips WHERE state IN ('planned','en_route')"
        parameters: list[Any] = []
        if corridor_id:
            query += " AND corridor_id=?"
            parameters.append(corridor_id)
        query += " ORDER BY trip_id"
        items = []
        with self.database.transaction() as conn:
            rows = conn.execute(query, parameters).fetchall()
        for row in rows:
            detail = self.trip_safety(row["trip_id"])
            items.append({
                "trip_id": detail["trip_id"],
                "state": detail["state"],
                "priority": detail["priority"],
                "anchor_km": detail["anchor_km"],
                "anchor_energy_kwh": detail["anchor_energy_kwh"],
                "safely_reachable": detail["safely_reachable"],
                "destination_margin_km": (detail.get("plan_path") or detail["reach"])
                ["destination"]["margin_km"],
                "has_active_reservation": "reservation" in detail,
            })
        return {
            "corridor_id": corridor_id,
            "total": len(items),
            "safe": sum(1 for item in items if item["safely_reachable"]),
            "at_risk": [item["trip_id"] for item in items if not item["safely_reachable"]],
            "items": items,
        }

    def trip_timeline(self, trip_id: str) -> dict[str, Any]:
        """回答"一次改派为何发生"：计划链与事件链按时间排列。"""

        with self.database.transaction() as conn:
            self._trip(conn, trip_id)
            plans = [
                {"plan_id": row["plan_id"], "plan_no": row["plan_no"],
                 "road_version": row["road_version"], "status": row["status"],
                 "valid_from": row["valid_from"], "valid_until": row["valid_until"],
                 "anchor_km": row["anchor_km"], "feasible": bool(row["feasible"]),
                 "supersede_reason": row["supersede_reason"],
                 "summary": json.loads(row["summary_json"])}
                for row in conn.execute(
                    "SELECT * FROM energy_plans WHERE trip_id=? ORDER BY plan_no", (trip_id,))
            ]
            events = [
                {"event_id": row["event_id"], "kind": row["kind"], "at_km": row["at_km"],
                 "from_plan_id": row["from_plan_id"], "to_plan_id": row["to_plan_id"],
                 "detail": json.loads(row["detail_json"]), "occurred_at": row["occurred_at"]}
                for row in conn.execute(
                    "SELECT * FROM trip_events WHERE trip_id=? ORDER BY occurred_at, created_at",
                    (trip_id,))
            ]
            return {"trip_id": trip_id, "plans": plans, "events": events}

    def station_power(self, station_id: str, at: str | None = None,
                      slots: int = 8) -> dict[str, Any]:
        """返回站点真实可用功率：检修、在线桩、分时限额与既有占用逐时隙对比。"""

        moment = charging.floor_slot(parse_dt(at)) if at else charging.floor_slot(self._now())
        with self.database.transaction() as conn:
            station = self._station(conn, station_id)
            charger_rows = conn.execute(
                "SELECT * FROM chargers WHERE station_id=? ORDER BY charger_id", (station_id,)
            ).fetchall()
            timeline = []
            for index in range(slots):
                slot = moment + timedelta(minutes=charging.SLOT_MINUTES * index)
                station_outage = self._in_outage(conn, "station_outages", "station_id",
                                                 station_id, slot)
                cap_window = conn.execute(
                    "SELECT MIN(cap_kw) AS cap FROM power_windows WHERE station_id=? "
                    "AND ?>=starts_at AND ?<ends_at",
                    (station_id, fmt_dt(slot), fmt_dt(slot)),
                ).fetchone()["cap"]
                open_chargers = []
                for charger in charger_rows:
                    if not charger["active"]:
                        continue
                    outage = self._in_outage(conn, "charger_outages", "charger_id",
                                             charger["charger_id"], slot)
                    busy = conn.execute(
                        "SELECT occupancy_state FROM slot_occupancy WHERE station_id=? "
                        "AND slot_start=? AND charger_id=?",
                        (station_id, fmt_dt(slot), charger["charger_id"]),
                    ).fetchone()
                    open_chargers.append({
                        "charger_id": charger["charger_id"],
                        "rated_power_kw": charger["rated_power_kw"],
                        "in_outage": outage,
                        "occupied": busy["occupancy_state"] if busy else None,
                        "available": (not outage and busy is None),
                    })
                rated_total = sum(c["rated_power_kw"] for c in open_chargers
                                  if not c["in_outage"])
                cap = rated_total if cap_window is None else min(rated_total, cap_window)
                used = conn.execute(
                    "SELECT COALESCE(SUM(draw_kw),0) AS used FROM slot_occupancy "
                    "WHERE station_id=? AND slot_start=?",
                    (station_id, fmt_dt(slot)),
                ).fetchone()["used"]
                timeline.append({
                    "slot_start": fmt_dt(slot),
                    "slot_end": fmt_dt(slot + timedelta(minutes=charging.SLOT_MINUTES)),
                    "station_in_outage": station_outage,
                    "window_cap_kw": cap_window,
                    "online_cap_kw": round(rated_total, 2),
                    "effective_cap_kw": round(0.0 if station_outage else cap, 2),
                    "used_kw": round(used, 2),
                    "available_kw": round(max(0.0, (0.0 if station_outage else cap) - used), 2),
                    "free_chargers": [c["charger_id"] for c in open_chargers if c["available"]],
                    "chargers": open_chargers,
                })
            return {"station_id": station_id, "active": bool(station["active"]),
                    "version": station["version"], "timeline": timeline}

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with self.database.transaction() as conn:
            return self._plan_detail(conn, plan_id)

    def _plan_detail(self, conn, plan_id: str) -> dict[str, Any]:
        plan = conn.execute("SELECT * FROM energy_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFoundError("补能计划不存在")
        summary = json.loads(plan["summary_json"])
        status = plan["status"]
        if status == "proposed" and self._now() > parse_dt(plan["valid_until"]):
            status = "expired"
        return {
            "plan_id": plan["plan_id"], "trip_id": plan["trip_id"], "plan_no": plan["plan_no"],
            "road_version": plan["road_version"], "status": status,
            "stored_status": plan["status"],
            "valid_from": plan["valid_from"], "valid_until": plan["valid_until"],
            "anchor_km": plan["anchor_km"], "anchor_at": plan["anchor_at"],
            "anchor_energy_kwh": plan["anchor_energy_kwh"],
            "feasible": bool(plan["feasible"]), "supersede_reason": plan["supersede_reason"],
            "margins": {
                "destination_energy_kwh": summary.get("destination_energy_kwh"),
                "dest_margin_kwh": summary.get("dest_margin_kwh"),
                "dest_margin_km": summary.get("dest_margin_km"),
                "time_margin_minutes": summary.get("time_margin_minutes"),
            },
            "legs": summary["legs"], "reasons": summary["reasons"],
            "total_charge_kwh": summary.get("total_charge_kwh"),
            "total_wait_minutes": summary.get("total_wait_minutes"),
        }

    # ------------------------------------------------------------------ 快照与读取

    def _snapshot(self, conn, *, corridor_id: str, road_version: int,
                  extra_occupancy: dict[tuple[str, datetime], dict] | None = None
                  ) -> charging.NetworkSnapshot:
        corridor = self._corridor(conn, corridor_id)
        station_rows = conn.execute(
            "SELECT * FROM charging_stations WHERE corridor_id=? ORDER BY position_km",
            (corridor_id,),
        ).fetchall()
        stations: list[charging.StationSpec] = []
        occupancy = self._load_occupancy(conn)
        if extra_occupancy:
            for key, value in extra_occupancy.items():
                bucket = occupancy.slot(key[0], key[1])
                if bucket.get("_virtual"):
                    continue
                merged = {"used_kw": bucket["used_kw"] + value["used_kw"],
                          "busy": {**bucket["busy"], **value["busy"]}, "_virtual": True}
                occupancy._slots[key] = merged
        for row in station_rows:
            chargers = tuple(
                charging.ChargerSpec(c["charger_id"], c["rated_power_kw"], bool(c["active"]))
                for c in conn.execute("SELECT * FROM chargers WHERE station_id=?",
                                      (row["station_id"],)).fetchall()
            )
            station_outages = tuple(
                charging.Outage(parse_dt(o["starts_at"]), parse_dt(o["ends_at"]))
                for o in conn.execute(
                    "SELECT * FROM station_outages WHERE station_id=?", (row["station_id"],))
            )
            charger_outages = tuple(
                (o["charger_id"], charging.Outage(parse_dt(o["starts_at"]), parse_dt(o["ends_at"])))
                for o in conn.execute(
                    "SELECT * FROM charger_outages WHERE charger_id IN "
                    "(SELECT charger_id FROM chargers WHERE station_id=?)",
                    (row["station_id"],))
            )
            windows = tuple(
                charging.PowerWindow(parse_dt(w["starts_at"]), parse_dt(w["ends_at"]),
                                     w["cap_kw"])
                for w in conn.execute(
                    "SELECT * FROM power_windows WHERE station_id=?", (row["station_id"],))
            )
            stations.append(charging.StationSpec(row["station_id"], row["position_km"],
                                                 bool(row["active"]), chargers, station_outages,
                                                 charger_outages, windows))
        segments = self._road_segments(conn, corridor_id, road_version)
        return charging.NetworkSnapshot(stations=stations, segments=segments,
                                        avg_speed_kmh=corridor["avg_speed_kmh"],
                                        occupancy=occupancy)

    def _road_segments(self, conn, corridor_id: str, version: int):
        return [
            charging.RoadSegment(r["from_km"], r["to_km"], r["open"], r["detour_extra_km"])
            for r in conn.execute(
                "SELECT * FROM road_segments WHERE corridor_id=? AND version=? ORDER BY from_km",
                (corridor_id, version))
        ]

    def _load_occupancy(self, conn) -> charging.OccupancyView:
        horizon = fmt_dt(charging.floor_slot(self._now()) - timedelta(minutes=charging.SLOT_MINUTES))
        slots: dict[tuple[str, datetime], dict] = {}
        rows = conn.execute(
            "SELECT * FROM slot_occupancy WHERE slot_start>=?", (horizon,)
        ).fetchall()
        for row in rows:
            key = (row["station_id"], parse_dt(row["slot_start"]))
            bucket = slots.setdefault(key, {"used_kw": 0.0, "busy": {}})
            bucket["used_kw"] += row["draw_kw"]
            bucket["busy"][row["charger_id"]] = row["draw_kw"]
        return charging.OccupancyView(slots)

    def _active_reservation(self, conn, trip_id: str):
        row = conn.execute("SELECT * FROM reservations WHERE trip_id=? AND status='active'",
                           (trip_id,)).fetchone()
        if row is None:
            raise ConflictError("该任务当前没有生效中的预约")
        return row

    def _reservation_leg(self, conn, reservation, station_id: str, states: tuple[str, ...]):
        row = conn.execute(
            "SELECT * FROM reservation_legs WHERE reservation_id=? AND station_id=? "
            "ORDER BY seq", (reservation["reservation_id"], station_id)).fetchone()
        if row is None:
            raise NotFoundError("预约中不存在该站点")
        if row["leg_state"] not in states:
            raise ConflictError(f"该站点预约段当前状态为 {row['leg_state']}")
        return row

    def _station(self, conn, station_id: str):
        row = conn.execute("SELECT * FROM charging_stations WHERE station_id=?",
                           (station_id,)).fetchone()
        if row is None:
            raise NotFoundError("充电站不存在")
        return row

    def _station_km(self, conn, station_id: str | None) -> float | None:
        if not station_id:
            return None
        row = conn.execute("SELECT position_km FROM charging_stations WHERE station_id=?",
                           (station_id,)).fetchone()
        if row is None:
            raise NotFoundError("充电站不存在")
        return row["position_km"]

    def _vehicle(self, conn, vehicle_id: str):
        row = conn.execute("SELECT * FROM vehicles WHERE vehicle_id=?", (vehicle_id,)).fetchone()
        if row is None:
            raise NotFoundError("车辆不存在")
        return row

    def _corridor(self, conn, corridor_id: str):
        row = conn.execute("SELECT * FROM corridors WHERE corridor_id=?", (corridor_id,)).fetchone()
        if row is None:
            raise NotFoundError("干线不存在")
        return row

    def _require_corridor_org(self, conn, actor: dict[str, Any], corridor_id: str) -> None:
        corridor = self._corridor(conn, corridor_id)
        if actor["organization_id"] != corridor["organization_id"] and actor["role"] != "admin":
            raise PermissionDenied("不能操作其他组织的干线")

    def _trip(self, conn, trip_id: str):
        row = conn.execute("SELECT * FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
        if row is None:
            raise NotFoundError("运输任务不存在")
        return row

    @staticmethod
    def _positive(value: float, field: str) -> None:
        if value is None or value <= 0:
            raise ValidationError(f"{field} 必须为正数")
