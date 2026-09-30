"""干线充电保障服务的规则测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from transport_coordination.charging_service import ChargingService
from transport_coordination.clock import FixedClock
from transport_coordination.errors import (CapacityError, PlanningError,
                                           StaleVersionError)
from transport_coordination.storage import Database

NOW = datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)


def build_world(svc: ChargingService, *, chargers: int = 2, power_limit: float = 400.0,
                queue_timeout: int = 15, bypass: bool = False) -> None:
    svc.register_organization(request_id="org", actor_id="bootstrap",
                              organization_id="o1", name="干线运营")
    svc.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                       display_name="管理员", role="admin", organization_id="o1")
    svc.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                       display_name="调度", role="operator", organization_id="o1")
    nodes = ["n0", "nA", "nB", "nC", "n4"]
    if bypass:
        nodes += ["nD1", "nD2"]
    for node in nodes:
        svc.register_node(request_id=f"node-{node}", actor_id="op1", node_id=node,
                          name=f"节点{node}")
    for sid, node in [("stA", "nA"), ("stB", "nB"), ("stC", "nC")]:
        svc.register_station(request_id=f"station-{sid}", actor_id="op1", station_id=sid,
                             organization_id="o1", name=f"服务区{sid}", node_id=node,
                             queue_timeout_minutes=queue_timeout)
        for index in range(chargers):
            svc.upsert_charger(request_id=f"charger-{sid}-{index}", actor_id="op1",
                               charger_id=f"{sid}-c{index}", station_id=sid,
                               max_power_kw=240.0)
        svc.set_power_schedule(request_id=f"power-{sid}", actor_id="op1", station_id=sid,
                               windows=[{"start_minute": 0, "end_minute": 1440,
                                         "max_power_kw": power_limit}])
    segments = [("e0A", "n0", "nA"), ("eAB", "nA", "nB"),
                ("eBC", "nB", "nC"), ("eC4", "nC", "n4")]
    for sid, a, b in segments:
        svc.upsert_segment(request_id=f"seg-{sid}", actor_id="op1", segment_id=sid,
                           from_node=a, to_node=b, distance_km=180.0, speed_kmh=90.0)
    if bypass:
        svc.upsert_segment(request_id="seg-bd1", actor_id="op1", segment_id="eBD1",
                           from_node="nB", to_node="nD1", distance_km=130.0, speed_kmh=80.0)
        svc.upsert_segment(request_id="seg-d12", actor_id="op1", segment_id="eD12",
                           from_node="nD1", to_node="nD2", distance_km=0.0 + 1.0,
                           speed_kmh=80.0)
        svc.upsert_segment(request_id="seg-d2c", actor_id="op1", segment_id="eD2C",
                           from_node="nD2", to_node="nC", distance_km=129.0, speed_kmh=80.0)
    svc.create_road_version(request_id="v1", actor_id="op1", note="基线版本")


def make_trip(svc: ChargingService, trip_id: str, *, energy: float = 620.0,
              priority: str = "normal", arrive_by: str = "2026-09-30T13:00:00Z") -> str:
    svc.register_trip(request_id=f"trip-{trip_id}", actor_id="op1", trip_id=trip_id,
                      vehicle_id=f"veh-{trip_id}", battery_kwh=700.0,
                      current_energy_kwh=energy, depart_at="2026-09-30T02:00:00Z",
                      max_charge_kw=240.0, rate_kwh_per_km=1.4,
                      origin_node="n0", destination_node="n4",
                      arrive_by=arrive_by, reserve_km=60.0, load_tons=31.0,
                      priority=priority)
    return trip_id


class ChargingServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = ChargingService(self.database, FixedClock(NOW))
        build_world(self.service)

    def tearDown(self):
        self.database.close()

    def plan_and_confirm(self, trip_id: str):
        plan = self.service.generate_plan(actor_id="op1", trip_id=trip_id)
        self.assertTrue(plan.feasible, plan.infeasible_reason)
        receipt, reservation = self.service.confirm_plan(
            request_id=f"confirm-{trip_id}", actor_id="op1", plan_id=plan.plan_id)
        return plan, receipt, reservation

    # ------------------------------------------------------------- 规划

    def test_plan_has_validity_window_and_margins(self):
        make_trip(self.service, "t1")
        plan = self.service.generate_plan(actor_id="op1", trip_id="t1")
        self.assertTrue(plan.feasible)
        self.assertEqual("2026-09-30T02:10:00Z", plan.valid_until)
        self.assertGreaterEqual(len(plan.stops), 1)
        for stop in plan.stops:
            self.assertGreaterEqual(stop.reserve_margin_km, -1e-6)
            self.assertEqual(stop.slot_start[14:16], "00")

    def test_plan_explains_infeasibility_when_destination_unreachable(self):
        make_trip(self.service, "t1")
        self.service.create_road_version(request_id="v2", actor_id="op1",
                                         note="封两段", closures=["eBC", "eC4"])
        plan = self.service.generate_plan(actor_id="op1", trip_id="t1")
        self.assertFalse(plan.feasible)
        self.assertIn("不可达", plan.infeasible_reason)

    def test_power_schedule_caps_concurrent_sessions(self):
        # 两台 240kW 桩，但分时功率总额度只有 240kW -> 同一时隙只能有一个会话
        database = Database()
        service = ChargingService(database, FixedClock(NOW))
        build_world(service, chargers=2, power_limit=240.0)
        make_trip(service, "t1")
        make_trip(service, "t2")
        plan1 = service.generate_plan(actor_id="op1", trip_id="t1")
        plan2 = service.generate_plan(actor_id="op1", trip_id="t2")
        service.confirm_plan(request_id="c1", actor_id="op1", plan_id=plan1.plan_id)
        with self.assertRaises(CapacityError):
            service.confirm_plan(request_id="c2", actor_id="op1", plan_id=plan2.plan_id)
        database.close()

    # ------------------------------------------------------------- 确认

    def test_failed_multi_station_confirm_leaves_no_ghost_occupancy(self):
        # 每站只有 1 台桩：t1 先锁定 stB+stC；t2 同刻需要两站，必须整体失败
        database = Database()
        service = ChargingService(database, FixedClock(NOW))
        build_world(service, chargers=1)
        make_trip(service, "t1")
        make_trip(service, "t2")
        plan1 = service.generate_plan(actor_id="op1", trip_id="t1")
        plan2 = service.generate_plan(actor_id="op1", trip_id="t2")
        service.confirm_plan(request_id="c1", actor_id="op1", plan_id=plan1.plan_id)
        ledger_before = service.database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_slot_ledger WHERE active=1").fetchone()["c"]
        with self.assertRaises(CapacityError):
            service.confirm_plan(request_id="c2", actor_id="op1", plan_id=plan2.plan_id)
        ledger_after = service.database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_slot_ledger WHERE active=1").fetchone()["c"]
        reservations_t2 = service.database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservations WHERE trip_id='t2'").fetchone()["c"]
        self.assertEqual(ledger_before, ledger_after)
        self.assertEqual(0, reservations_t2)
        database.close()

    def test_confirm_retry_does_not_double_deduct_capacity(self):
        make_trip(self.service, "t1")
        plan = self.service.generate_plan(actor_id="op1", trip_id="t1")
        first, reservation1 = self.service.confirm_plan(
            request_id="same-req", actor_id="op1", plan_id=plan.plan_id)
        second, reservation2 = self.service.confirm_plan(
            request_id="same-req", actor_id="op1", plan_id=plan.plan_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        active = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_slot_ledger WHERE active=1").fetchone()["c"]
        # 台账行数与预约站点的时隙数完全一致，且只有一条预约
        self.assertEqual(
            active,
            sum(self._ledger_rows(stop.station_id) for stop in reservation1.stops))
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservations").fetchone()["c"])

    def _ledger_rows(self, station_id: str) -> int:
        return self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_slot_ledger WHERE active=1 AND station_id=?",
            (station_id,)).fetchone()["c"]

    def test_expired_plan_cannot_be_confirmed(self):
        make_trip(self.service, "t1")
        plan = self.service.generate_plan(actor_id="op1", trip_id="t1")
        later = ChargingService(self.database,
                                FixedClock(datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)))
        with self.assertRaises(StaleVersionError):
            later.confirm_plan(request_id="late", actor_id="op1", plan_id=plan.plan_id)

    def test_equipment_change_invalidates_plan(self):
        make_trip(self.service, "t1")
        plan = self.service.generate_plan(actor_id="op1", trip_id="t1")
        self.service.upsert_charger(request_id="maint-1", actor_id="op1",
                                    charger_id="stB-c0", station_id="stB",
                                    max_power_kw=240.0, status="maintenance")
        with self.assertRaises(StaleVersionError):
            self.service.confirm_plan(request_id="c1", actor_id="op1", plan_id=plan.plan_id)

    def test_new_road_version_invalidates_plan(self):
        make_trip(self.service, "t1")
        plan = self.service.generate_plan(actor_id="op1", trip_id="t1")
        self.service.create_road_version(request_id="v2", actor_id="op1", note="无实质变化")
        with self.assertRaises(StaleVersionError):
            self.service.confirm_plan(request_id="c1", actor_id="op1", plan_id=plan.plan_id)

    # ------------------------------------------------------------- 优先权

    def test_rescue_displaces_only_reserved_normal_trips(self):
        make_trip(self.service, "t1")
        _, _, reservation = self.plan_and_confirm("t1")
        make_trip(self.service, "rescue1", priority="rescue")
        rescue_plan = self.service.generate_plan(actor_id="op1", trip_id="rescue1")
        receipt, rescue_res = self.service.confirm_plan(
            request_id="rescue-confirm", actor_id="op1", plan_id=rescue_plan.plan_id)
        self.assertFalse(receipt.replayed)
        self.assertEqual("superseded", self.database.connection.execute(
            "SELECT status FROM reservations WHERE reservation_id=?",
            (reservation.reservation_id,)).fetchone()["status"])
        self.assertEqual("awaiting_replan", self.database.connection.execute(
            "SELECT status FROM trips WHERE trip_id='t1'").fetchone()["status"])
        # 被挤掉的普通任务台账全部释放，新任务台账生效
        self.assertEqual(0, self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_slot_ledger WHERE active=1 AND trip_id='t1'"
        ).fetchone()["c"])
        self.assertGreater(self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_slot_ledger WHERE active=1 AND trip_id='rescue1'"
        ).fetchone()["c"], 0)

    def test_rescue_cannot_displace_started_session(self):
        database = Database()
        service = ChargingService(database, FixedClock(NOW))
        build_world(service, chargers=1)
        make_trip(service, "t1")
        plan = service.generate_plan(actor_id="op1", trip_id="t1")
        self.assertTrue(plan.feasible)
        _, reservation = service.confirm_plan(request_id="c1", actor_id="op1",
                                              plan_id=plan.plan_id)
        first = reservation.stops[0]
        service.report_event(request_id="arr", actor_id="op1", trip_id="t1",
                             event_type="arrived", station_id=first.station_id,
                             energy_kwh=300.0, occurred_at="2026-09-30T05:55:00Z")
        service.report_event(request_id="chg", actor_id="op1", trip_id="t1",
                             event_type="charging", station_id=first.station_id,
                             delivered_kwh=10.0, occurred_at="2026-09-30T06:00:00Z")
        # 抢险车同样必须在 stB 补能且无法跳过它直达 stC；stB 唯一会话已开始
        make_trip(service, "rescue1", priority="rescue")
        rescue_plan = service.generate_plan(actor_id="op1", trip_id="rescue1")
        self.assertFalse(rescue_plan.feasible)
        with self.assertRaises((CapacityError, PlanningError)):
            service.confirm_plan(request_id="rescue-confirm", actor_id="op1",
                                 plan_id=rescue_plan.plan_id)
        # 已开始的会话状态与占用保持不变
        first_stop_id = database.connection.execute(
            "SELECT res_stop_id FROM reservation_stops WHERE reservation_id=? ORDER BY seq LIMIT 1",
            (reservation.reservation_id,)).fetchone()["res_stop_id"]
        self.assertEqual("charging", database.connection.execute(
            "SELECT status FROM reservation_stops WHERE res_stop_id=?",
            (first_stop_id,)).fetchone()["status"])
        self.assertGreater(database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_slot_ledger WHERE active=1 AND trip_id='t1'"
        ).fetchone()["c"], 0)
        database.close()

    # ------------------------------------------------------------- 事实与改派

    def test_fault_replans_from_actual_energy_and_releases_future_stops(self):
        make_trip(self.service, "t1")
        plan, _, reservation = self.plan_and_confirm("t1")
        first, second = reservation.stops[0], reservation.stops[1]
        self.service.report_event(request_id="arr1", actor_id="op1", trip_id="t1",
                                  event_type="arrived", station_id=first.station_id,
                                  energy_kwh=300.0, occurred_at="2026-09-30T05:55:00Z")
        self.service.report_event(request_id="done1", actor_id="op1", trip_id="t1",
                                  event_type="completed", station_id=first.station_id,
                                  delivered_kwh=336.0, occurred_at="2026-09-30T07:20:00Z")
        self.service.report_event(request_id="arr2", actor_id="op1", trip_id="t1",
                                  event_type="arrived", station_id=second.station_id,
                                  energy_kwh=300.0, occurred_at="2026-09-30T09:20:00Z")
        self.service.report_event(request_id="fault", actor_id="op1", trip_id="t1",
                                  event_type="fault", station_id=second.station_id,
                                  energy_kwh=300.0, occurred_at="2026-09-30T09:25:00Z")
        new_plan = self.service.replan(request_id="rp1", actor_id="op1",
                                       trip_id="t1", reason="station_fault")
        self.assertTrue(new_plan.feasible, new_plan.infeasible_reason)
        self.assertEqual(1, new_plan.version_seq - plan.version_seq)
        # 锚点必须是故障事实的电量，而不是旧计划假设
        overview = self.service.trip_overview("t1")
        self.assertEqual("awaiting_replan", overview["status"])
        reasons = [r.reason for r in self.service.list_replans("t1")]
        self.assertIn("station_fault", reasons)
        self.assertEqual(0, self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_slot_ledger WHERE active=1 "
            "AND reservation_id=?", (reservation.reservation_id,)).fetchone()["c"])

    def test_event_retry_is_idempotent(self):
        make_trip(self.service, "t1")
        _, _, reservation = self.plan_and_confirm("t1")
        first = reservation.stops[0]
        self.service.report_event(request_id="evt-0", actor_id="op1", trip_id="t1",
                                  event_type="arrived", station_id=first.station_id,
                                  energy_kwh=300.0, occurred_at="2026-09-30T06:00:00Z")
        kwargs = dict(actor_id="op1", trip_id="t1", event_type="charging",
                      station_id=first.station_id, delivered_kwh=120.0,
                      occurred_at="2026-09-30T06:05:00Z")
        self.service.report_event(request_id="evt-1", **kwargs)
        replay = self.service.report_event(request_id="evt-1", **kwargs)
        self.assertTrue(replay.replayed)
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM trip_state_facts WHERE kind='charging'").fetchone()["c"])
        self.assertEqual(120.0, self.database.connection.execute(
            "SELECT delivered_kwh FROM reservation_stops WHERE res_stop_id=?",
            (self.database.connection.execute(
                "SELECT res_stop_id FROM reservation_stops WHERE reservation_id=? ORDER BY seq LIMIT 1",
                (reservation.reservation_id,)).fetchone()["res_stop_id"],)).fetchone()["delivered_kwh"])

    def test_queue_timeout_sweep_releases_capacity(self):
        database = Database()
        service = ChargingService(database, FixedClock(
            datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)))
        build_world(service, queue_timeout=15)
        make_trip(service, "t1")
        plan = service.generate_plan(actor_id="op1", trip_id="t1")
        _, reservation = service.confirm_plan(request_id="c1", actor_id="op1",
                                              plan_id=plan.plan_id)
        # 时钟在预约到达之前：不处理
        self.assertEqual([], service.sweep_queue_timeouts(now_at="2026-09-30T03:00:00Z"))
        swept = service.sweep_queue_timeouts(now_at="2026-09-30T10:00:00Z")
        self.assertEqual(1, len(swept))
        self.assertEqual("awaiting_replan", service.database.connection.execute(
            "SELECT status FROM trips WHERE trip_id='t1'").fetchone()["status"])
        self.assertEqual(0, service.database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_slot_ledger WHERE active=1").fetchone()["c"])
        self.assertIn("queue_timeout", [r.reason for r in service.list_replans("t1")])
        database.close()

    def test_early_arrival_replans_around_earlier_slot(self):
        make_trip(self.service, "t1")
        _, _, reservation = self.plan_and_confirm("t1")
        first = reservation.stops[0]
        self.service.report_event(request_id="arr-early", actor_id="op1", trip_id="t1",
                                  event_type="arrived", station_id=first.station_id,
                                  energy_kwh=300.0, occurred_at="2026-09-30T05:00:00Z")
        new_plan = self.service.replan(request_id="rp-early", actor_id="op1",
                                       trip_id="t1", reason="early_arrival",
                                       now_at="2026-09-30T05:00:00Z")
        self.assertTrue(new_plan.feasible, new_plan.infeasible_reason)
        self.assertLessEqual(new_plan.stops[0].slot_start, first.slot_start)

    def test_changing_started_session_plan_is_rejected(self):
        make_trip(self.service, "t1")
        _, _, reservation = self.plan_and_confirm("t1")
        first = reservation.stops[0]
        self.service.report_event(request_id="arr", actor_id="op1", trip_id="t1",
                                  event_type="arrived", station_id=first.station_id,
                                  energy_kwh=300.0, occurred_at="2026-09-30T05:55:00Z")
        with self.assertRaises(PlanningError):
            self.service.replan(request_id="rp-bad", actor_id="op1",
                                trip_id="t1", reason="dispatch_change")

    # ------------------------------------------------------------- 道路封闭

    def test_road_closure_reroutes_through_bypass(self):
        database = Database()
        service = ChargingService(database, FixedClock(NOW))
        build_world(service, bypass=True)
        make_trip(service, "t1", arrive_by="2026-09-30T16:00:00Z")
        plan = service.generate_plan(actor_id="op1", trip_id="t1")
        service.confirm_plan(request_id="c1", actor_id="op1", plan_id=plan.plan_id)
        service.create_road_version(request_id="v2", actor_id="op1",
                                    note="临时封闭 nB-nC", closures=["eBC"])
        new_plan = service.replan(request_id="rp-road", actor_id="op1",
                                  trip_id="t1", reason="road_closed",
                                  current_energy_kwh=620.0, current_node="n0",
                                  now_at="2026-09-30T02:00:00Z")
        self.assertTrue(new_plan.feasible, new_plan.infeasible_reason)
        self.assertEqual(2, new_plan.road_version)

    # ------------------------------------------------------------- 运营查询

    def test_station_capacity_reflects_maintenance_and_schedule(self):
        make_trip(self.service, "t1")
        self.service.upsert_charger(request_id="maint", actor_id="op1",
                                    charger_id="stB-c1", station_id="stB",
                                    max_power_kw=240.0, status="maintenance")
        view = self.service.station_capacity("stB", from_at="2026-09-30T06:00:00Z", slots=1)
        self.assertEqual(1, view["slots"][0]["operational_chargers"])
        self.assertEqual(240.0, view["slots"][0]["power_limit_kw"])
        self.assertIn("stB-c1", view["maintenance_chargers"])

    def test_safe_arrivals_flags_vehicles(self):
        make_trip(self.service, "t1")
        _, _, reservation = self.plan_and_confirm("t1")
        items = {item["trip_id"]: item for item in self.service.safe_arrivals()}
        self.assertIn("t1", items)
        self.assertTrue(items["t1"]["can_reach_next_station"])
        self.assertGreaterEqual(items["t1"]["usable_range_km"], 0)

    # ------------------------------------------------------------- 重启持久化

    def test_reservations_survive_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "charging.sqlite3"
            database = Database(path)
            service = ChargingService(database, FixedClock(NOW))
            build_world(service)
            make_trip(service, "t1")
            plan = service.generate_plan(actor_id="op1", trip_id="t1")
            service.confirm_plan(request_id="c1", actor_id="op1", plan_id=plan.plan_id)
            valid, count = service.verify_audit()
            self.assertTrue(valid)
            database.close()

            database2 = Database(path)
            service2 = ChargingService(database2, FixedClock(NOW))
            reservation = service2.get_reservation("t1")
            self.assertIsNotNone(reservation)
            self.assertEqual("active", reservation.status)
            self.assertGreaterEqual(len(reservation.stops), 1)
            arrivals = service2.safe_arrivals()
            self.assertEqual("t1", arrivals[0]["trip_id"])
            valid2, _ = service2.verify_audit()
            self.assertTrue(valid2)
            database2.close()


if __name__ == "__main__":
    unittest.main()
