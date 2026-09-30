import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from transport_coordination.charging_service import ChargingService
from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, PermissionDenied
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

from tests.charging_world import ChargingWorld, DAY


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class PlanLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.world = ChargingWorld()

    def tearDown(self):
        self.world.close()

    def test_plan_has_validity_window_and_margins(self):
        self.world.trip()
        plan = self.world.svc.plan_energy(request_id="p1", actor_id="op", trip_id="trip1")
        self.assertEqual("proposed", plan["status"])
        self.assertTrue(plan["valid_until"] > plan["valid_from"])
        self.assertIn("dest_margin_kwh", plan["margins"])
        self.assertIn("time_margin_minutes", plan["margins"])
        for leg in plan["legs"]:
            self.assertIn("arrive_margin_km", leg)
            self.assertIn("power_margin_kw", leg)
            self.assertTrue(leg["draws"])

    def test_confirm_locks_multiple_stations_atomically_and_replay_deducts_once(self):
        self.world.trip()
        plan, confirmation = self.world.plan_and_confirm()
        self.assertEqual(2, confirmation["legs"])
        locked = self.world.database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"]
        self.assertEqual(confirmation["locked_slots"], locked)
        self.assertGreater(locked, 0)

        # 同一请求重试：返回同一预约，占用不翻倍
        replay = self.world.svc.confirm_plan(request_id="confirm-1", actor_id="op",
                                             plan_id=plan["plan_id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(confirmation["reservation_id"], replay["reservation_id"])
        self.assertEqual(locked, self.world.database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"])

    def test_failed_confirm_leaves_no_ghost_occupancy(self):
        self.world.trip()
        plan = self.world.svc.plan_energy(request_id="p1", actor_id="op", trip_id="trip1")
        # 确认前对 B 站追加覆盖其全部预约时隙的全站检修
        b_leg = next(leg for leg in plan["legs"] if leg["station_id"] == "st-b")
        self.world.svc.add_station_outage(
            request_id="outage-b", actor_id="op", station_id="st-b",
            starts_at=f"{DAY}T00:00Z", ends_at=f"{DAY}T23:59Z", reason="突发故障")
        with self.assertRaises(ConflictError):
            self.world.svc.confirm_plan(request_id="c1", actor_id="op",
                                        plan_id=plan["plan_id"])
        self.assertEqual(0, self.world.database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"])
        self.assertEqual(0, self.world.database.connection.execute(
            "SELECT COUNT(*) AS c FROM reservations WHERE status='active'").fetchone()["c"])

    def test_expired_plan_cannot_be_confirmed(self):
        self.world.trip()
        plan = self.world.svc.plan_energy(request_id="p1", actor_id="op", trip_id="trip1")
        self.world.clock._value = utc(plan["valid_until"])
        from datetime import timedelta
        self.world.clock._value += timedelta(minutes=1)
        with self.assertRaises(ConflictError):
            self.world.svc.confirm_plan(request_id="c1", actor_id="op",
                                        plan_id=plan["plan_id"])
        self.assertEqual(0, self.world.database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"])

    def test_other_driver_cannot_confirm_trip(self):
        self.world.trip()
        plan = self.world.svc.plan_energy(request_id="p1", actor_id="op", trip_id="trip1")
        with self.assertRaises(PermissionDenied):
            self.world.svc.confirm_plan(request_id="c1", actor_id="drv2",
                                        plan_id=plan["plan_id"])


class RescuePriorityTest(unittest.TestCase):
    def _single_charger_world(self):
        world = ChargingWorld()
        # ca2 全天检修，A 站只剩 ca1，迫使两类车辆在 A 使用同一物理时隙
        world.svc.add_charger_outage(
            request_id="ca2-out", actor_id="op", charger_id="ca2",
            starts_at=f"{DAY}T00:00Z", ends_at=f"{DAY}T23:59Z", reason="检修")
        return world

    def test_rescue_displaces_unstarted_normal_reservation(self):
        world = self._single_charger_world()
        world.vehicle("truck-r", priority="rescue")
        world.svc.create_trip(
            request_id="trip-rescue", actor_id="op", trip_id="trip-r", corridor_id="g4",
            vehicle_id="truck-r", driver_actor_id="drv2", origin_km=0, destination_km=400,
            load_tonnes=30, initial_energy_kwh=380, deadline=f"{DAY}T18:00Z",
            departure_at=f"{DAY}T06:00Z")
        world.trip("trip-n", vehicle_id="truck1", driver="drv")
        # 抢险先生成计划但不确认
        rescue_plan = world.svc.plan_energy(request_id="rp", actor_id="op", trip_id="trip-r")
        _, normal_conf = world.plan_and_confirm("trip-n", "np", "nc")
        # 抢险确认：挤掉尚未开始的普通预约
        rescue_conf = world.svc.confirm_plan(request_id="rc", actor_id="op",
                                             plan_id=rescue_plan["plan_id"])
        normal = world.database.connection.execute(
            "SELECT status FROM reservations WHERE reservation_id=?",
            (normal_conf["reservation_id"],)).fetchone()
        self.assertEqual("displaced_by_rescue", normal["status"])
        # 普通车的占用全部释放，抢险占用生效
        normal_occ = world.database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy WHERE reservation_id=?",
            (normal_conf["reservation_id"],)).fetchone()["c"]
        rescue_occ = world.database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy WHERE reservation_id=?",
            (rescue_conf["reservation_id"],)).fetchone()["c"]
        self.assertEqual(0, normal_occ)
        self.assertGreater(rescue_occ, 0)
        # 被挤车辆自动得到新的待确认计划，且新计划不再撞抢险时隙
        new_plan = world.svc.trip_timeline("trip-n")["plans"][-1]
        self.assertEqual("proposed", new_plan["status"])
        rescue_slots = {(leg["station_id"], slot) for leg in rescue_plan["legs"]
                        for slot, _ in leg["draws"]}
        for leg in new_plan["summary"]["legs"]:
            for slot, _ in leg["draws"]:
                self.assertNotIn((leg["station_id"], slot), rescue_slots)
        world.close()

    def test_rescue_cannot_displace_started_session_and_rolls_back(self):
        world = self._single_charger_world()
        world.vehicle("truck-r", priority="rescue")
        world.svc.create_trip(
            request_id="trip-rescue", actor_id="op", trip_id="trip-r", corridor_id="g4",
            vehicle_id="truck-r", driver_actor_id="drv2", origin_km=0, destination_km=400,
            load_tonnes=30, initial_energy_kwh=380, deadline=f"{DAY}T18:00Z",
            departure_at=f"{DAY}T06:00Z")
        world.trip("trip-n", vehicle_id="truck1", driver="drv")
        rescue_plan = world.svc.plan_energy(request_id="rp", actor_id="op", trip_id="trip-r")
        _, normal_conf = world.plan_and_confirm("trip-n", "np", "nc")

        # 普通车进入 A 站并开始充电会话
        world.clock._value = utc(f"{DAY}T07:45Z")
        world.svc.mark_arrival(request_id="na", actor_id="drv", trip_id="trip-n",
                               station_id="st-a", occurred_at=f"{DAY}T07:45Z")
        world.clock._value = utc(f"{DAY}T08:00Z")
        world.svc.start_session(request_id="ns", actor_id="drv", trip_id="trip-n",
                                station_id="st-a", started_at=f"{DAY}T08:00Z")
        occupied_before = world.database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"]

        with self.assertRaises(ConflictError):
            world.svc.confirm_plan(request_id="rc", actor_id="op",
                                   plan_id=rescue_plan["plan_id"])
        # 抢险事务整体回滚：未留下任何抢险占用，普通车会话与占用原样保留
        self.assertEqual(0, world.database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy WHERE reservation_id NOT IN "
            "(SELECT reservation_id FROM reservations WHERE reservation_id=?)",
            (normal_conf["reservation_id"],)).fetchone()["c"])
        self.assertEqual(occupied_before, world.database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"])
        session = world.database.connection.execute(
            "SELECT session_state FROM charging_sessions WHERE reservation_id=?",
            (normal_conf["reservation_id"],)).fetchone()
        self.assertEqual("charging", session["session_state"])
        world.close()


class ReplanningTest(unittest.TestCase):
    def setUp(self):
        self.world = ChargingWorld()
        self.world.trip()
        self.plan, self.conf = self.world.plan_and_confirm()

    def tearDown(self):
        self.world.close()

    def _complete_a(self, delivered: float):
        self.world.clock._value = utc(f"{DAY}T07:45Z")
        self.world.svc.mark_arrival(request_id="arr", actor_id="drv", trip_id="trip1",
                                    station_id="st-a", occurred_at=f"{DAY}T07:45Z")
        self.world.clock._value = utc(f"{DAY}T08:00Z")
        self.world.svc.start_session(request_id="st", actor_id="drv", trip_id="trip1",
                                     station_id="st-a", started_at=f"{DAY}T08:00Z")
        self.world.clock._value = utc(f"{DAY}T09:00Z")
        self.world.svc.complete_session(request_id="done", actor_id="drv", trip_id="trip1",
                                        station_id="st-a", delivered_kwh=delivered,
                                        ended_at=f"{DAY}T09:00Z")

    def test_fault_replans_from_actual_delivered_energy(self):
        planned_a = next(leg for leg in self.plan["legs"] if leg["station_id"] == "st-a")
        partial = round(planned_a["charge_kwh"] * 0.4, 3)
        self._start_a()
        self.world.clock._value = utc(f"{DAY}T08:20Z")
        result = self.world.svc.report_event(
            request_id="fault", actor_id="drv", trip_id="trip1", kind="fault",
            at_km=120, occurred_at=f"{DAY}T08:20Z",
            detail={"station_id": "st-a", "delivered_kwh": partial})
        new_plan = self.world.svc.get_plan(result["plan_id"])
        self.assertEqual(120, new_plan["anchor_km"])
        # 锚点电量 = 初始 380 - 120km*1.8 + 实充 partial
        self.assertAlmostEqual(380 - 216 + partial, new_plan["anchor_energy_kwh"], places=2)
        session = self.world.database.connection.execute(
            "SELECT session_state, delivered_kwh FROM charging_sessions").fetchone()
        self.assertEqual("faulted", session["session_state"])
        self.assertAlmostEqual(partial, session["delivered_kwh"])
        timeline = self.world.svc.trip_timeline("trip1")
        self.assertIn("fault", [e["kind"] for e in timeline["events"]])

    def _start_a(self):
        self.world.clock._value = utc(f"{DAY}T07:45Z")
        self.world.svc.mark_arrival(request_id="arr", actor_id="drv", trip_id="trip1",
                                    station_id="st-a", occurred_at=f"{DAY}T07:45Z")
        self.world.clock._value = utc(f"{DAY}T08:00Z")
        self.world.svc.start_session(request_id="st", actor_id="drv", trip_id="trip1",
                                     station_id="st-a", started_at=f"{DAY}T08:00Z")

    def test_queue_timeout_replans_and_releases_future_legs(self):
        self.world.clock._value = utc(f"{DAY}T08:30Z")
        result = self.world.svc.report_event(
            request_id="qt", actor_id="drv", trip_id="trip1", kind="queue_timeout",
            at_km=119, occurred_at=f"{DAY}T08:30Z",
            detail={"station_id": "st-a", "waited_minutes": 45})
        self.assertTrue(result["feasible"])
        self.assertEqual("superseded", self.world.database.connection.execute(
            "SELECT status FROM reservations WHERE reservation_id=?",
            (self.conf["reservation_id"],)).fetchone()["status"])
        self.assertEqual(0, self.world.database.connection.execute(
            "SELECT COUNT(*) AS c FROM slot_occupancy WHERE reservation_id=?",
            (self.conf["reservation_id"],)).fetchone()["c"])

    def test_early_arrival_rejected_by_arrival_then_replanned_as_event(self):
        self.world.clock._value = utc(f"{DAY}T07:20Z")
        with self.assertRaises(ConflictError):
            self.world.svc.mark_arrival(request_id="early", actor_id="drv", trip_id="trip1",
                                        station_id="st-a", occurred_at=f"{DAY}T07:20Z")
        result = self.world.svc.report_event(
            request_id="ea", actor_id="drv", trip_id="trip1", kind="early_arrival",
            at_km=120, occurred_at=f"{DAY}T07:20Z", detail={"station_id": "st-a"})
        new_plan = self.world.svc.get_plan(result["plan_id"])
        # 提前到站后新计划应在原 08:00 时隙之前就安排充电
        self.assertLess(new_plan["legs"][0]["slot_start"], f"{DAY}T08:00:00Z")

    def test_road_closure_replans_on_new_version_with_detour(self):
        self._complete_a(delivered=next(
            leg for leg in self.plan["legs"] if leg["station_id"] == "st-a")["charge_kwh"])
        self.world.publish_road(2, [(0, 200, True, 0), (200, 230, False, 30),
                                    (230, 400, True, 0)], "临时封闭")
        self.world.clock._value = utc(f"{DAY}T09:05Z")
        result = self.world.svc.report_event(
            request_id="close", actor_id="drv", trip_id="trip1", kind="road_closure",
            at_km=120, occurred_at=f"{DAY}T09:05Z", detail={"note": "前方施工"})
        new_plan = self.world.svc.get_plan(result["plan_id"])
        self.assertEqual(2, new_plan["road_version"])
        self.assertTrue(result["feasible"])

    def test_session_completion_is_idempotent(self):
        planned_a = next(leg for leg in self.plan["legs"] if leg["station_id"] == "st-a")
        self._complete_a(planned_a["charge_kwh"])
        replay = self.world.svc.complete_session(
            request_id="done", actor_id="drv", trip_id="trip1", station_id="st-a",
            delivered_kwh=planned_a["charge_kwh"], ended_at=f"{DAY}T09:00Z")
        self.assertTrue(replay["replayed"])
        total = self.world.database.connection.execute(
            "SELECT COALESCE(SUM(delivered_kwh),0) AS t FROM charging_sessions").fetchone()["t"]
        self.assertAlmostEqual(planned_a["charge_kwh"], total)


class OperationsQueryTest(unittest.TestCase):
    def setUp(self):
        self.world = ChargingWorld()
        self.world.trip()
        self.plan, self.conf = self.world.plan_and_confirm()

    def tearDown(self):
        self.world.close()

    def test_station_power_reflects_outage_window_cap_and_occupancy(self):
        power = self.world.svc.station_power("st-a", at=f"{DAY}T08:00Z", slots=4)
        slots = power["timeline"]
        # 08:00 起 A 站两台 120kW 桩，本任务占一台，另一台空闲
        self.assertEqual(240, slots[0]["online_cap_kw"])
        self.assertEqual(120, slots[0]["used_kw"])
        self.assertEqual(120, slots[0]["available_kw"])
        self.assertEqual(1, len(slots[0]["free_chargers"]))

        # 加入 150kW 分时限额与一台桩检修（覆盖本任务 09:00 的占用时隙）
        self.world.svc.add_power_window(
            request_id="pw", actor_id="op", station_id="st-a",
            starts_at=f"{DAY}T09:00Z", ends_at=f"{DAY}T09:30Z", cap_kw=150, note="午峰")
        self.world.svc.add_charger_outage(
            request_id="co", actor_id="op", charger_id="ca1",
            starts_at=f"{DAY}T09:00Z", ends_at=f"{DAY}T09:30Z", reason="检修")
        power = self.world.svc.station_power("st-a", at=f"{DAY}T09:00Z", slots=1)
        first = power["timeline"][0]
        self.assertEqual(120, first["online_cap_kw"])            # 仅 ca2 在线
        self.assertEqual(120, first["effective_cap_kw"])         # min(120, 150)
        self.assertEqual(120, first["used_kw"])                  # 既有预约锁定的功率
        self.assertEqual(0, first["available_kw"])               # 限额下已无余量
        by_id = {c["charger_id"]: c for c in first["chargers"]}
        # 检修在预约锁定之后追加：ca1 既检修又被占用，正是运营需要改派的信号
        self.assertTrue(by_id["ca1"]["in_outage"])
        self.assertIsNotNone(by_id["ca1"]["occupied"])
        self.assertEqual(["ca2"], first["free_chargers"])

    def test_timeline_explains_why_reassignment_happened(self):
        self.world.clock._value = utc(f"{DAY}T08:30Z")
        self.world.svc.report_event(
            request_id="qt", actor_id="drv", trip_id="trip1", kind="queue_timeout",
            at_km=119, occurred_at=f"{DAY}T08:30Z", detail={"station_id": "st-a"})
        timeline = self.world.svc.trip_timeline("trip1")
        kinds = [event["kind"] for event in timeline["events"]]
        self.assertIn("plan_confirmed", kinds)
        replanned = [e for e in timeline["events"] if e["kind"] == "plan_replanned"][-1]
        self.assertEqual("queue_timeout", replanned["detail"]["trigger"])
        statuses = [plan["status"] for plan in timeline["plans"]]
        self.assertIn("superseded", statuses)
        self.assertEqual("proposed", statuses[-1])

    def test_safety_shows_reachability_after_replan(self):
        safety = self.world.svc.trip_safety("trip1")
        self.assertIn("reach", safety)
        self.assertIn("destination", safety["reach"])
        self.assertTrue(safety["safely_reachable"])

    def test_safety_board_lists_which_vehicles_still_reach_destination(self):
        # 第二趟任务初始电量过低，续航内没有充电站 -> 不可行 -> 风险车辆
        self.world.vehicle("truck2")
        self.world.svc.create_trip(
            request_id="trip-risky", actor_id="op", trip_id="trip-risky", corridor_id="g4",
            vehicle_id="truck2", driver_actor_id="drv2", origin_km=0, destination_km=400,
            load_tonnes=30, initial_energy_kwh=150, deadline=f"{DAY}T18:00Z",
            departure_at=f"{DAY}T06:00Z")
        board = self.world.svc.safety_board("g4")
        self.assertEqual(2, board["total"])
        self.assertEqual(1, board["safe"])
        self.assertEqual(["trip-risky"], board["at_risk"])
        by_trip = {item["trip_id"]: item for item in board["items"]}
        self.assertTrue(by_trip["trip1"]["has_active_reservation"])
        self.assertFalse(by_trip["trip-risky"]["has_active_reservation"])


class PersistenceTest(unittest.TestCase):
    def test_unarrived_reservation_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.sqlite3"
            world = ChargingWorld(path)
            world.trip()
            plan, confirmation = world.plan_and_confirm()
            occupied = world.database.connection.execute(
                "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"]
            world.database.close()

            database = Database(path)
            clock = FixedClock(utc(f"{DAY}T07:45Z"))
            base = DomainService(database, clock)
            svc = ChargingService(database, clock)
            active = database.connection.execute(
                "SELECT status FROM reservations WHERE reservation_id=?",
                (confirmation["reservation_id"],)).fetchone()
            self.assertEqual("active", active["status"])
            self.assertEqual(occupied, database.connection.execute(
                "SELECT COUNT(*) AS c FROM slot_occupancy").fetchone()["c"])
            # 重启后可继续完成到站与会话流程
            svc.mark_arrival(request_id="arr2", actor_id="drv", trip_id="trip1",
                             station_id="st-a", occurred_at=f"{DAY}T07:45Z")
            leg = database.connection.execute(
                "SELECT leg_state FROM reservation_legs WHERE reservation_id=? AND seq=1",
                (confirmation["reservation_id"],)).fetchone()
            self.assertEqual("arrived", leg["leg_state"])
            database.close()


if __name__ == "__main__":
    unittest.main()
