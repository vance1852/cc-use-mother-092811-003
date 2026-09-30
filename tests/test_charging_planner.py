import unittest
from datetime import datetime, timezone, timedelta

from transport_coordination import charging as ch


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 30, hour, minute, tzinfo=timezone.utc)


def vehicle(**overrides):
    defaults = dict(battery_kwh=400, kwh_per_km_empty=1.2, kwh_per_km_loaded=1.8,
                    rated_payload_tonnes=30, reserve_kwh=40)
    defaults.update(overrides)
    return ch.VehicleSpec(**defaults)


def station(station_id="s1", position=100.0, chargers=None, outages=(),
            charger_outages=(), windows=()):
    chargers = chargers if chargers is not None else (
        ch.ChargerSpec("c1", 120, True), ch.ChargerSpec("c2", 120, True))
    return ch.StationSpec(station_id, position, True, tuple(chargers), tuple(outages),
                          tuple(charger_outages), tuple(windows))


def network(stations, segments=None, speed=60.0, occupancy=None):
    segments = segments if segments is not None else [ch.RoadSegment(0, 500, 1, 0)]
    return ch.NetworkSnapshot(stations=stations, segments=segments, avg_speed_kmh=speed,
                              occupancy=occupancy)


class PlannerModelTest(unittest.TestCase):
    def test_consumption_scales_with_load(self):
        net = network([station()])
        spec = vehicle()
        self.assertAlmostEqual(1.2, net.consumption(spec, 0))
        self.assertAlmostEqual(1.5, net.consumption(spec, 15))
        self.assertAlmostEqual(1.8, net.consumption(spec, 30))
        # 超载按满载封顶，不外推
        self.assertAlmostEqual(1.8, net.consumption(spec, 60))

    def test_detour_adds_distance_only_inside_closed_segment(self):
        seg = [ch.RoadSegment(0, 100, 1, 0), ch.RoadSegment(100, 130, 0, 30),
               ch.RoadSegment(130, 500, 1, 0)]
        net = network([station()], seg)
        self.assertAlmostEqual(140, net.path_distance(90, 200))
        self.assertAlmostEqual(50, net.path_distance(0, 50))

    def test_station_outage_and_charger_outage_zero_power(self):
        net = network([
            station("a", 100, outages=(ch.Outage(dt(8), dt(12)),)),
            station("b", 200, charger_outages=(("c1", ch.Outage(dt(8), dt(12))),)),
        ])
        a, b = net.stations
        self.assertEqual(0.0, net.slot_power(a, a.chargers[0], dt(9)))
        self.assertEqual(0.0, net.slot_power(b, b.chargers[0], dt(9)))
        self.assertAlmostEqual(120, net.slot_power(b, b.chargers[1], dt(9)))
        # 检修窗口外恢复
        self.assertAlmostEqual(120, net.slot_power(a, a.chargers[0], dt(13)))

    def test_power_window_caps_and_existing_occupancy(self):
        occ = ch.OccupancyView({
            ("a", dt(9)): {"used_kw": 100.0, "busy": {"c1": 100.0}},
        })
        net = network([station("a", 100, windows=(ch.PowerWindow(dt(8), dt(12), 150),))],
                      occupancy=occ)
        a = net.stations[0]
        # c1 物理占用
        self.assertEqual(0.0, net.slot_power(a, a.chargers[0], dt(9)))
        # c2 受站点限额 150 - 已用 100 = 50
        self.assertAlmostEqual(50, net.slot_power(a, a.chargers[1], dt(9)))
        # 窗口外恢复额定
        self.assertAlmostEqual(120, net.slot_power(a, a.chargers[1], dt(13)))


class PlannerTripTest(unittest.TestCase):
    def test_direct_reach_needs_no_leg_and_reports_margins(self):
        net = network([station("a", 100), station("b", 250)])
        outcome = ch.plan_trip(vehicle=vehicle(), load_tonnes=30, network=net,
                               anchor_km=0, anchor_at=dt(6), anchor_energy_kwh=400,
                               destination_km=200, deadline=dt(18))
        self.assertTrue(outcome.feasible)
        self.assertEqual(0, len(outcome.legs))
        # 200km * 1.8 = 360，剩余 40 恰好保底，余量 0
        self.assertAlmostEqual(40, outcome.destination_energy_kwh)
        self.assertAlmostEqual(0, outcome.dest_margin_kwh)

    def test_long_trip_charges_at_farthest_station_and_reports_margins(self):
        net = network([station("a", 120), station("b", 250)])
        outcome = ch.plan_trip(vehicle=vehicle(), load_tonnes=30, network=net,
                               anchor_km=0, anchor_at=dt(6), anchor_energy_kwh=380,
                               destination_km=400, deadline=dt(18))
        self.assertTrue(outcome.feasible, outcome.reasons)
        self.assertEqual(["a", "b"], [leg.station_id for leg in outcome.legs])
        first = outcome.legs[0]
        self.assertLess(0, first.arrive_margin_kwh)
        self.assertLess(0, first.power_margin_kw + 1e-9)
        # 到 A 站能耗 216，余 164，保底 40 -> 余量 124kWh ≈ 68.9km
        self.assertAlmostEqual(124, first.arrive_margin_kwh, places=2)
        self.assertGreaterEqual(outcome.time_margin_minutes, 0)

    def test_infeasible_when_no_station_in_range(self):
        net = network([station("late", 300)])
        outcome = ch.plan_trip(vehicle=vehicle(), load_tonnes=30, network=net,
                               anchor_km=0, anchor_at=dt(6), anchor_energy_kwh=200,
                               destination_km=400, deadline=dt(18))
        self.assertFalse(outcome.feasible)
        self.assertTrue(any("续航" in reason for reason in outcome.reasons))

    def test_outage_forces_wait_or_other_charger(self):
        # A 站仅一台桩，08-12 检修；车辆 08:00 到站，必须等到 12:00 才能补能
        only = station("a", 120, chargers=(ch.ChargerSpec("c1", 120, True),),
                       charger_outages=(("c1", ch.Outage(dt(8), dt(12))),))
        net = network([only, station("b", 250)])
        outcome = ch.plan_trip(vehicle=vehicle(), load_tonnes=30, network=net,
                               anchor_km=0, anchor_at=dt(6), anchor_energy_kwh=380,
                               destination_km=400, deadline=dt(20))
        self.assertTrue(outcome.feasible, outcome.reasons)
        self.assertGreaterEqual(outcome.legs[0].slot_start, dt(12))

    def test_deadline_miss_marked_infeasible(self):
        net = network([station("a", 120)])
        outcome = ch.plan_trip(vehicle=vehicle(), load_tonnes=30, network=net,
                               anchor_km=0, anchor_at=dt(6), anchor_energy_kwh=300,
                               destination_km=400, deadline=dt(10))
        self.assertFalse(outcome.feasible)


class ReachTest(unittest.TestCase):
    def test_evaluate_reach_flags_stations_and_destination(self):
        net = network([station("a", 120), station("b", 250)])
        result = ch.evaluate_reach(vehicle=vehicle(), load_tonnes=30, network=net,
                                   anchor_km=0, anchor_at=dt(6), anchor_energy_kwh=260,
                                   destination_km=400)
        by_id = {item["station_id"]: item for item in result["stations"]}
        self.assertTrue(by_id["a"]["safely_reachable"])
        # 250km 需要 450 + 保底 40 = 490 > 260
        self.assertFalse(by_id["b"]["safely_reachable"])
        self.assertFalse(result["destination"]["safely_reachable"])


if __name__ == "__main__":
    unittest.main()
