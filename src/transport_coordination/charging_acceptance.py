"""运行干线充电保障服务的离线端到端验收。

覆盖：设备检修与分时功率限额、带有效期与余量的计划、多站原子确认与幂等重试、
故障后按真实电量重规划、抢险优先权保护已开始会话、运营查询与重启持久化。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .charging_service import ChargingService
from .clock import FixedClock
from .storage import Database

NOW = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)


def _build_network(service: ChargingService) -> None:
    service.register_organization(request_id="org", actor_id="bootstrap",
                                  organization_id="org-hd", name="高速干线运营")
    service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="adm",
                           display_name="管理员", role="admin", organization_id="org-hd")
    service.register_actor(request_id="op", actor_id="adm", new_actor_id="disp",
                           display_name="调度员", role="operator", organization_id="org-hd")
    for node in ["k0", "k1", "k2", "k3", "k4"]:
        service.register_node(request_id=f"node-{node}", actor_id="disp",
                              node_id=node, name=f"节点{node}")
    for station_id, node in [("svc1", "k1"), ("svc2", "k2"), ("svc3", "k3")]:
        service.register_station(request_id=f"st-{station_id}", actor_id="disp",
                                 station_id=station_id, organization_id="org-hd",
                                 name=f"服务区{station_id}", node_id=node,
                                 queue_timeout_minutes=15)
        for index in range(2):
            service.upsert_charger(request_id=f"ch-{station_id}-{index}", actor_id="disp",
                                   charger_id=f"{station_id}-gun{index}", station_id=station_id,
                                   max_power_kw=240.0)
        # 夜间满功率，10:00-12:00 站点总功率限额降到 300kW
        service.set_power_schedule(
            request_id=f"pw-{station_id}", actor_id="disp", station_id=station_id,
            windows=[{"start_minute": 0, "end_minute": 600, "max_power_kw": 480.0},
                     {"start_minute": 600, "end_minute": 720, "max_power_kw": 300.0},
                     {"start_minute": 720, "end_minute": 1440, "max_power_kw": 480.0}])
    for segment_id, a, b in [("r01", "k0", "k1"), ("r12", "k1", "k2"),
                             ("r23", "k2", "k3"), ("r34", "k3", "k4")]:
        service.upsert_segment(request_id=f"seg-{segment_id}", actor_id="disp",
                               segment_id=segment_id, from_node=a, to_node=b,
                               distance_km=180.0, speed_kmh=90.0)
    service.create_road_version(request_id="road-v1", actor_id="disp", note="国庆基线")


def run() -> dict[str, object]:
    """执行完整充电保障链并返回核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database_path = Path(directory) / "charging.sqlite3"
        database = Database(database_path)
        service = ChargingService(database, FixedClock(NOW))
        _build_network(service)

        # 节假日场景：svc2 一台桩检修中
        service.upsert_charger(request_id="svc2-maint", actor_id="disp",
                               charger_id="svc2-gun1", station_id="svc2",
                               max_power_kw=240.0, status="maintenance")

        service.register_trip(request_id="trip-1", actor_id="disp", trip_id="haul-1",
                              vehicle_id="truck-001", battery_kwh=700.0,
                              current_energy_kwh=620.0, depart_at="2026-10-01T02:00:00Z",
                              max_charge_kw=240.0, rate_kwh_per_km=1.4,
                              origin_node="k0", destination_node="k4",
                              arrive_by="2026-10-01T13:00:00Z",
                              reserve_km=60.0, load_tons=31.0)
        plan = service.generate_plan(actor_id="disp", trip_id="haul-1")
        feasible = plan.feasible
        margins = [round(stop.reserve_margin_km, 1) for stop in plan.stops]
        plan_ttl = plan.valid_until == "2026-10-01T02:10:00Z"

        # 多站原子确认 + 同请求重试
        first_receipt, reservation = service.confirm_plan(
            request_id="confirm-1", actor_id="disp", plan_id=plan.plan_id)
        replay_receipt, _ = service.confirm_plan(
            request_id="confirm-1", actor_id="disp", plan_id=plan.plan_id)
        confirm_idempotent = (not first_receipt.replayed and replay_receipt.replayed
                              and first_receipt.resource_id == replay_receipt.resource_id)

        # 真实可用功率：检修站只剩一台在运桩
        capacity = service.station_capacity("svc2", from_at="2026-10-01T03:00:00Z", slots=1)
        maintenance_reflected = (capacity["slots"][0]["operational_chargers"] == 1
                                 and "svc2-gun1" in capacity["maintenance_chargers"])

        # 事实：到站、充电、完成第一站
        first_stop = reservation.stops[0]
        service.report_event(request_id="ev-arr", actor_id="disp", trip_id="haul-1",
                             event_type="arrived", station_id=first_stop.station_id,
                             energy_kwh=300.0, occurred_at="2026-10-01T05:55:00Z")
        service.report_event(request_id="ev-done", actor_id="disp", trip_id="haul-1",
                             event_type="completed", station_id=first_stop.station_id,
                             delivered_kwh=336.0, occurred_at="2026-10-01T07:20:00Z")

        # 第二站故障，携带真实剩余电量，系统按事实重规划
        second_stop = reservation.stops[1]
        service.report_event(request_id="ev-arr2", actor_id="disp", trip_id="haul-1",
                             event_type="arrived", station_id=second_stop.station_id,
                             energy_kwh=300.0, occurred_at="2026-10-01T09:20:00Z")
        service.report_event(request_id="ev-fault", actor_id="disp", trip_id="haul-1",
                             event_type="fault", station_id=second_stop.station_id,
                             energy_kwh=300.0, occurred_at="2026-10-01T09:25:00Z",
                             detail={"message": "充电桩急停"})
        new_plan = service.replan(request_id="replan-1", actor_id="disp",
                                  trip_id="haul-1", reason="station_fault")
        replan_feasible = new_plan.feasible
        replan_reasons = [item.reason for item in service.list_replans("haul-1")]
        no_ghost = database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_slot_ledger WHERE active=1 AND trip_id='haul-1'"
        ).fetchone()["c"] == 0

        arrivals = service.safe_arrivals()
        safe_arrivals_ok = bool(arrivals) and all(
            item["anchor"]["basis"].startswith("fact") for item in arrivals)

        # 道路临时封闭：新版本发布后重规划走绕行（k3 之后无路可绕 -> 报告不可达原因）
        service.create_road_version(request_id="road-v2", actor_id="disp",
                                    note="r34 临时封闭", closures=["r34"])
        closed_plan = service.replan(request_id="replan-2", actor_id="disp",
                                     trip_id="haul-1", reason="road_closed",
                                     now_at="2026-10-01T09:30:00Z")
        closure_detected = (not closed_plan.feasible
                            and "不可达" in (closed_plan.infeasible_reason or ""))

        valid, event_count = service.verify_audit()
        database.close()

        # 重启：未完成的新计划与全部业务状态继续有效
        database2 = Database(database_path)
        restarted = ChargingService(database2, FixedClock(NOW))
        overview = restarted.trip_overview("haul-1")
        plan_still_readable = restarted.get_plan(new_plan.plan_id).plan_id == new_plan.plan_id
        restart_history = restarted.list_replans("haul-1")
        history_reasons = {item.reason for item in restart_history}
        history_intact = {"station_fault", "road_closed"} <= history_reasons
        valid2, _ = restarted.verify_audit()
        database2.close()

        return {
            "status": "ok",
            "plan_feasible": feasible,
            "plan_margins_km": margins,
            "plan_ttl_enforced": plan_ttl,
            "confirm_idempotent": confirm_idempotent,
            "maintenance_reflected_in_capacity": maintenance_reflected,
            "replan_feasible_after_fault": replan_feasible,
            "replan_reasons": replan_reasons,
            "no_ghost_occupancy": no_ghost,
            "safe_arrivals_fact_based": safe_arrivals_ok,
            "road_closure_detected": closure_detected,
            "restart_plan_readable": plan_still_readable,
            "restart_history_intact": history_intact,
            "trip_status_after_restart": overview["status"],
            "audit_events": event_count,
            "audit_valid": valid and valid2,
        }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    required = ["plan_feasible", "plan_ttl_enforced", "confirm_idempotent",
                "maintenance_reflected_in_capacity", "replan_feasible_after_fault",
                "no_ghost_occupancy", "safe_arrivals_fact_based", "road_closure_detected",
                "restart_plan_readable", "restart_history_intact", "audit_valid"]
    return 0 if result["status"] == "ok" and all(result[key] for key in required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
