import unittest

from transport_coordination.api import route
from transport_coordination.charging_service import ChargingService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

from tests.charging_world import ChargingWorld, DAY


def call(world, method, path, body=None, actor="op"):
    return route(world.base, method, path, body or {}, {"X-Actor-Id": actor})


class ChargingApiTest(unittest.TestCase):
    def setUp(self):
        self.world = ChargingWorld()

    def tearDown(self):
        self.world.close()

    def test_full_charging_flow_over_http_routes(self):
        status, body = call(self.world, "POST", "/trips", {"request_id": "t1"})
        # 缺少必填字段 -> 400 invalid_request
        self.assertEqual(400, status)

        self.world.vehicle("truck1")
        status, trip = call(self.world, "POST", "/trips", {
            "request_id": "t1", "trip_id": "trip1", "corridor_id": "g4",
            "vehicle_id": "truck1", "driver_actor_id": "drv",
            "origin_km": 0, "destination_km": 400, "load_tonnes": 30,
            "initial_energy_kwh": 380, "deadline": f"{DAY}T18:00Z",
            "departure_at": f"{DAY}T06:00Z"})
        self.assertEqual(201, status)

        status, plan = call(self.world, "POST", "/energy-plans",
                            {"request_id": "p1", "trip_id": "trip1"})
        self.assertEqual(201, status)
        self.assertTrue(plan["feasible"])
        self.assertIn("valid_until", plan)

        status, confirmation = call(self.world, "POST", "/plan-confirmations",
                                    {"request_id": "c1", "plan_id": plan["plan_id"]})
        self.assertEqual(201, status)
        self.assertEqual(2, confirmation["legs"])

        # 幂等重试
        status, replay = call(self.world, "POST", "/plan-confirmations",
                              {"request_id": "c1", "plan_id": plan["plan_id"]})
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        # 运营查询
        status, safety = call(self.world, "GET", "/trips/trip1/safety")
        self.assertEqual(200, status)
        self.assertTrue(safety["safely_reachable"])
        self.assertIn("plan_path", safety)

        status, timeline = call(self.world, "GET", "/trips/trip1/timeline")
        self.assertEqual(200, status)
        self.assertTrue(any(e["kind"] == "plan_confirmed" for e in timeline["events"]))

        status, power = call(self.world, "GET", "/stations/st-a/power?at=" + f"{DAY}T08:00Z")
        self.assertEqual(200, status)
        self.assertEqual(8, len(power["timeline"]))

        status, fetched = call(self.world, "GET", f"/plans/{plan['plan_id']}")
        self.assertEqual(200, status)
        self.assertEqual("confirmed", fetched["status"])

    def test_unknown_charging_route_404(self):
        status, body = route(self.world.base, "GET", "/nope", None, {})
        self.assertEqual(404, status)

    def test_event_replan_route(self):
        self.world.trip()
        self.world.plan_and_confirm()
        status, body = call(self.world, "POST", "/trip-events", {
            "request_id": "e1", "trip_id": "trip1", "kind": "queue_timeout",
            "at_km": 119, "occurred_at": f"{DAY}T08:30Z",
            "detail": {"station_id": "st-a"}})
        self.assertEqual(201, status)
        self.assertTrue(body["feasible"])

    def test_station_power_requires_existing_station(self):
        status, body = call(self.world, "GET", "/stations/missing/power")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"])


if __name__ == "__main__":
    unittest.main()
