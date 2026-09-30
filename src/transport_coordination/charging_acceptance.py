"""运行干线充电保障的离线端到端验收。

剧本覆盖节假日高压场景：服务区充电桩检修叠加站点功率限额、补能计划有效期与余量、
多站时隙原子锁定与请求重试幂等、抢险优先挤占未开始预约但不得挤掉已开始会话、
故障按已完成充电事实重规划、运营安全/改派/真实功率查询，以及进程重启后预约继续有效。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .charging_service import ChargingService
from .clock import FixedClock
from .errors import ConflictError
from .service import DomainService
from .storage import Database

DAY = "2026-09-30"


def build_world(path: Path) -> tuple[ChargingService, DomainService, Database, FixedClock]:
    """搭建标准干线：400km、A 站 120km、B 站 250km，各 2 台 120kW 桩。"""

    clock = FixedClock(datetime(2026, 9, 30, 6, 0, tzinfo=timezone.utc))
    database = Database(path)
    base = DomainService(database, clock)
    svc = ChargingService(database, clock)
    base.register_organization(request_id="org", actor_id="bootstrap",
                               organization_id="o1", name="干线运营")
    base.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm",
                        display_name="管理员", role="admin", organization_id="o1")
    base.register_actor(request_id="op", actor_id="adm", new_actor_id="op",
                        display_name="调度", role="operator", organization_id="o1")
    base.register_actor(request_id="drv", actor_id="adm", new_actor_id="drv",
                        display_name="司机甲", role="reviewer", organization_id="o1")
    base.register_actor(request_id="drv2", actor_id="adm", new_actor_id="drv2",
                        display_name="司机乙", role="reviewer", organization_id="o1")
    svc.register_corridor(request_id="cor", actor_id="op", corridor_id="g4",
                          display_name="示范干线", length_km=400, avg_speed_kmh=60)
    svc.register_station(request_id="sta", actor_id="op", station_id="st-a",
                         corridor_id="g4", display_name="A 服务区", position_km=120)
    svc.register_station(request_id="stb", actor_id="op", station_id="st-b",
                         corridor_id="g4", display_name="B 服务区", position_km=250)
    for key, sid in [("ca1", "st-a"), ("ca2", "st-a"), ("cb1", "st-b"), ("cb2", "st-b")]:
        svc.register_charger(request_id=f"ch-{key}", actor_id="op", charger_id=key,
                             station_id=sid, rated_power_kw=120)
    svc.publish_road_version(
        request_id="road-1", actor_id="op", corridor_id="g4", version=1,
        segments=[{"from_km": 0, "to_km": 400, "open": True, "detour_extra_km": 0}],
        note="全程通行")

    def vehicle(vehicle_id: str, priority: str = "normal") -> None:
        svc.register_vehicle(
            request_id=f"veh-{vehicle_id}", actor_id="op", vehicle_id=vehicle_id,
            display_name=vehicle_id, battery_kwh=400,
            kwh_per_km_empty=1.2, kwh_per_km_loaded=1.8,
            rated_payload_tonnes=30, reserve_kwh=40, priority=priority)

    def trip(trip_id: str, vehicle_id: str, *, driver: str, deadline: str = f"{DAY}T18:00Z",
             request_id: str | None = None) -> None:
        svc.create_trip(
            request_id=request_id or f"trip-{trip_id}", actor_id="op", trip_id=trip_id,
            corridor_id="g4", vehicle_id=vehicle_id, driver_actor_id=driver,
            origin_km=0, destination_km=400, load_tonnes=30, initial_energy_kwh=380,
            deadline=deadline, departure_at=f"{DAY}T06:00Z")

    return svc, base, database, clock, vehicle, trip  # type: ignore[return-value]


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "charging.sqlite3"
        svc, base, database, clock, vehicle, trip = build_world(path)

        # 节假日条件：A 站一台桩全天检修（只剩 ca1），A 站 06-18 全站功率限额 150kW
        svc.add_charger_outage(
            request_id="holiday-ca2", actor_id="op", charger_id="ca2",
            starts_at=f"{DAY}T00:00Z", ends_at=f"{DAY}T23:59Z", reason="节假日检修")
        svc.add_power_window(
            request_id="holiday-cap", actor_id="op", station_id="st-a",
            starts_at=f"{DAY}T06:00Z", ends_at=f"{DAY}T18:00Z", cap_kw=150,
            note="节假日午峰限额")

        # 三趟运输（普通 + 两辆抢险）在调度阶段并行生成计划，随后依次确认
        vehicle("truck1")
        trip("trip-n", "truck1", driver="drv")
        vehicle("truck-r", priority="rescue")
        trip("trip-r", "truck-r", driver="drv2", request_id="trip-r")
        vehicle("truck-r2", priority="rescue")
        trip("trip-r2", "truck-r2", driver="drv2", request_id="trip-r2")
        normal_plan = svc.plan_energy(request_id="np", actor_id="op", trip_id="trip-n")
        rescue_plan = svc.plan_energy(request_id="rp", actor_id="op", trip_id="trip-r")
        rescue2_plan = svc.plan_energy(request_id="rp2", actor_id="op", trip_id="trip-r2")
        assert normal_plan["feasible"] and rescue_plan["feasible"] and rescue2_plan["feasible"]
        assert normal_plan["valid_until"] > normal_plan["valid_from"]
        # 普通车先确认，占住共享时隙；重试必须幂等
        normal_conf = svc.confirm_plan(request_id="nc", actor_id="op",
                                       plan_id=normal_plan["plan_id"])
        replay = svc.confirm_plan(request_id="nc", actor_id="op",
                                  plan_id=normal_plan["plan_id"])
        assert replay["replayed"] and replay["reservation_id"] == normal_conf["reservation_id"]
        occupied_after_confirm = database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"]
        replay_after = database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"]
        # 抢险确认时挤掉尚未开始的普通预约
        rescue_conf = svc.confirm_plan(request_id="rc", actor_id="op",
                                       plan_id=rescue_plan["plan_id"])
        displaced_status = database.connection.execute(
            "SELECT status FROM reservations WHERE reservation_id=?",
            (normal_conf["reservation_id"],)).fetchone()["status"]
        assert displaced_status == "displaced_by_rescue"
        displaced_occ = database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy WHERE reservation_id=?",
            (normal_conf["reservation_id"],)).fetchone()["c"]
        assert displaced_occ == 0
        # 被挤车辆得到不撞抢险时隙的新计划
        normal_new = svc.trip_timeline("trip-n")["plans"][-1]
        rescue_slots = {(leg["station_id"], slot) for leg in rescue_plan["legs"]
                        for slot, _ in leg["draws"]}
        for leg in normal_new["summary"]["legs"]:
            for slot, _ in leg["draws"]:
                assert (leg["station_id"], slot) not in rescue_slots

        # 抢险车辆在 A 站开始充电会话；第二个抢险请求不得挤掉，必须整体回滚
        svc.mark_arrival(request_id="ra", actor_id="drv2", trip_id="trip-r",
                         station_id="st-a", occurred_at=f"{DAY}T07:45Z")
        svc.start_session(request_id="rs", actor_id="drv2", trip_id="trip-r",
                          station_id="st-a", started_at=f"{DAY}T08:00Z")
        blocked = False
        try:
            svc.confirm_plan(request_id="rc2", actor_id="op",
                             plan_id=rescue2_plan["plan_id"])
        except ConflictError:
            blocked = True
        assert blocked
        rescue_rows = database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservations WHERE trip_id='trip-r2'").fetchone()["c"]
        assert rescue_rows == 0  # 失败事务回滚，未留下抢险预约
        assert database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"] == \
            database.connection.execute(
                "SELECT COUNT(*) AS c FROM slot_occupancy WHERE reservation_id=?",
                (rescue_conf["reservation_id"],)).fetchone()["c"]
        session_state = database.connection.execute(
            "SELECT session_state FROM charging_sessions WHERE reservation_id=?",
            (rescue_conf["reservation_id"],)).fetchone()["session_state"]
        assert session_state == "charging"

        # 故障：按实充 40kWh 的事实在 A 站重规划
        fault = svc.report_event(
            request_id="fault", actor_id="drv2", trip_id="trip-r", kind="fault",
            at_km=120, occurred_at=f"{DAY}T08:20Z",
            detail={"station_id": "st-a", "delivered_kwh": 40.0})
        assert fault["feasible"]
        fault_plan = svc.get_plan(fault["plan_id"])
        assert abs(fault_plan["anchor_energy_kwh"] - (380 - 216 + 40)) < 0.01
        # 确认故障后的新计划，形成一个未到站的 B 站预约
        svc.confirm_plan(request_id="fc", actor_id="op", plan_id=fault["plan_id"])

        # 临时道路封闭：发布版本 2
        svc.publish_road_version(
            request_id="road-2", actor_id="op", corridor_id="g4", version=2,
            segments=[{"from_km": 0, "to_km": 200, "open": True, "detour_extra_km": 0},
                      {"from_km": 200, "to_km": 230, "open": False, "detour_extra_km": 30},
                      {"from_km": 230, "to_km": 400, "open": True, "detour_extra_km": 0}],
            note="节假日临时施工封闭")
        closure = svc.report_event(
            request_id="closure", actor_id="drv2", trip_id="trip-r", kind="road_closure",
            at_km=120, occurred_at=f"{DAY}T08:30Z", detail={"note": "前方施工"})
        assert closure["feasible"]
        assert svc.get_plan(closure["plan_id"])["road_version"] == 2
        # 司机确认封闭后的新计划，形成一个未到站的生效预约
        svc.confirm_plan(request_id="cc", actor_id="op", plan_id=closure["plan_id"])

        # 运营查询：安全可达、真实可用功率、改派原因
        safety = svc.trip_safety("trip-r")
        power = svc.station_power("st-a", at=f"{DAY}T08:00Z", slots=2)
        timeline = svc.trip_timeline("trip-r")
        reasons = [e["detail"].get("trigger") for e in timeline["events"]
                   if e["kind"] == "plan_replanned"]
        assert "fault" in reasons and "road_closure" in reasons
        assert power["timeline"][0]["effective_cap_kw"] <= 150

        active_before_restart = database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservations WHERE status='active'").fetchone()["c"]
        occupied_before_restart = database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"]
        audit_valid, audit_events = base.verify_audit()
        database.close()

        # 进程重启：未到站预约与占用继续有效
        database = Database(path)
        restarted = ChargingService(database, FixedClock(clock.now()))
        active_after = database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservations WHERE status='active'").fetchone()["c"]
        occupied_after = database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"]
        assert active_after == active_before_restart == 1
        assert occupied_after == occupied_before_restart
        # 重启后运营接口仍可判断安全可达
        assert "safely_reachable" in restarted.trip_safety("trip-r")
        valid_after, _ = DomainService(database, clock).verify_audit()
        database.close()

        return {
            "status": "ok",
            "audit_valid": audit_valid and valid_after,
            "audit_events": audit_events,
            "normal_reservation": normal_conf["reservation_id"],
            "normal_displaced": displaced_status == "displaced_by_rescue",
            "confirm_replayed": replay["replayed"],
            "idempotent_occupancy": occupied_after_confirm == replay_after,
            "rescue_started_session_protected": blocked,
            "fault_replan_feasible": fault["feasible"],
            "fault_anchor_kwh": round(fault_plan["anchor_energy_kwh"], 3),
            "road_versions": 2,
            "safety_safely_reachable": safety["safely_reachable"],
            "station_effective_cap_kw": power["timeline"][0]["effective_cap_kw"],
            "replan_reasons": reasons,
            "active_reservations_after_restart": active_after,
            "occupancy_after_restart": occupied_after,
        }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
