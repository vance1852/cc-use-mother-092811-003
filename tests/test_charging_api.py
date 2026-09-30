import unittest

from transport_coordination.api import route
from transport_coordination.charging_service import ChargingService
from transport_coordination.clock import FixedClock
from transport_coordination.storage import Database
from datetime import datetime, timezone


def setup_charging(service: ChargingService) -> None:
    service.register_organization(request_id="org", actor_id="bootstrap",
                                  organization_id="o1", name="干线运营")
    service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                           display_name="管理员", role="admin", organization_id="o1")
    service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                           display_name="调度", role="operator", organization_id="o1")
    for node in ["n0", "n1", "n2"]:
        service.register_node(request_id=f"node-{node}", actor_id="op1",
                              node_id=node, name=f"节点{node}")
    service.register_station(request_id="st1", actor_id="op1", station_id="st1",
                             organization_id="o1", name="服务区一", node_id="n1",
                             queue_timeout_minutes=15)
    service.upsert_charger(request_id="ch1", actor_id="op1", charger_id="gun1",
                           station_id="st1", max_power_kw=240.0)
    service.set_power_schedule(request_id="pw1", actor_id="op1", station_id="st1",
                               windows=[{"start_minute": 0, "end_minute": 1440,
                                         "max_power_kw": 400.0}])
    service.upsert_segment(request_id="seg1", actor_id="op1", segment_id="e01",
                           from_node="n0", to_node="n1", distance_km=180.0, speed_kmh=90.0)
    service.upsert_segment(request_id="seg2", actor_id="op1", segment_id="e12",
                           from_node="n1", to_node="n2", distance_km=180.0, speed_kmh=90.0)
    service.create_road_version(request_id="v1", actor_id="op1", note="基线")


class ChargingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = ChargingService(
            self.database, FixedClock(datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)))
        setup_charging(self.service)
        self.headers = {"X-Actor-Id": "op1"}

    def tearDown(self):
        self.database.close()

    def test_full_charging_flow_over_http(self):
        status, body = route(self.service, "POST", "/trips", {
            "request_id": "trip-1", "trip_id": "t1", "vehicle_id": "v1",
            "battery_kwh": 700.0, "current_energy_kwh": 620.0,
            "depart_at": "2026-10-01T02:00:00Z", "max_charge_kw": 240.0,
            "rate_kwh_per_km": 1.4, "origin_node": "n0", "destination_node": "n2",
            "arrive_by": "2026-10-01T13:00:00Z", "reserve_km": 60.0, "load_tons": 31.0,
        }, self.headers)
        self.assertEqual(201, status)

        status, plan = route(self.service, "POST", "/trips/t1/plans", {}, self.headers)
        self.assertEqual(201, status)
        self.assertTrue(plan["feasible"])
        self.assertIn("valid_until", plan)

        status, body = route(self.service, "POST", f"/plans/{plan['plan_id']}/confirm",
                             {"request_id": "confirm-1"}, self.headers)
        self.assertEqual(201, status)
        self.assertEqual("active", body["reservation"]["status"])

        # 同一 request_id 重试返回 200（回放），不重复锁定
        status, body = route(self.service, "POST", f"/plans/{plan['plan_id']}/confirm",
                             {"request_id": "confirm-1"}, self.headers)
        self.assertEqual(200, status)
        self.assertTrue(body["receipt"]["replayed"])

        status, body = route(self.service, "GET",
                             "/stations/st1/capacity?from_at=2026-10-01T03:00:00Z&slots=2",
                             None, self.headers)
        self.assertEqual(200, status)
        self.assertEqual(2, len(body["slots"]))

        status, body = route(self.service, "GET", "/ops/safe-arrivals", None, self.headers)
        self.assertEqual(200, status)
        self.assertEqual("t1", body["items"][0]["trip_id"])

        status, body = route(self.service, "GET", "/trips/t1/replans", None, self.headers)
        self.assertEqual(200, status)
        self.assertEqual([], body["items"])

    def test_capacity_endpoint_unknown_station_404(self):
        status, body = route(self.service, "GET", "/stations/nope/capacity",
                             None, self.headers)
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"])

    def test_report_event_requires_matching_reservation(self):
        status, body = route(self.service, "POST", "/trips/t1/events", {
            "request_id": "ev1", "event_type": "arrived", "station_id": "st1",
        }, self.headers)
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
