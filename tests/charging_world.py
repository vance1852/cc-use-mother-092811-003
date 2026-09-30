"""充电保障测试共用的建档工厂。"""

from datetime import datetime, timezone

from transport_coordination.charging_service import ChargingService
from transport_coordination.clock import FixedClock
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

DAY = "2026-09-30"


class ChargingWorld:
    """搭建一条标准干线：400km、A 站 120km、B 站 250km，各 2 台 120kW 桩。"""

    def __init__(self, path: str = ":memory:", when: datetime | None = None):
        self.database = Database(path)
        self.clock = FixedClock(when or datetime(2026, 9, 30, 6, 0, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.svc = ChargingService(self.database, self.clock)
        self._bootstrap()

    def _bootstrap(self) -> None:
        b = self.base
        s = self.svc
        b.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="干线运营")
        b.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm",
                         display_name="管理员", role="admin", organization_id="o1")
        b.register_actor(request_id="op", actor_id="adm", new_actor_id="op",
                         display_name="调度", role="operator", organization_id="o1")
        b.register_actor(request_id="drv", actor_id="adm", new_actor_id="drv",
                         display_name="司机", role="reviewer", organization_id="o1")
        b.register_actor(request_id="drv2", actor_id="adm", new_actor_id="drv2",
                         display_name="司机二", role="reviewer", organization_id="o1")
        s.register_corridor(request_id="cor", actor_id="op", corridor_id="g4",
                            display_name="示范干线", length_km=400, avg_speed_kmh=60)
        s.register_station(request_id="sta", actor_id="op", station_id="st-a",
                           corridor_id="g4", display_name="A 服务区", position_km=120)
        s.register_station(request_id="stb", actor_id="op", station_id="st-b",
                           corridor_id="g4", display_name="B 服务区", position_km=250)
        for key, sid in [("ca1", "st-a"), ("ca2", "st-a"), ("cb1", "st-b"), ("cb2", "st-b")]:
            s.register_charger(request_id=f"ch-{key}", actor_id="op", charger_id=key,
                               station_id=sid, rated_power_kw=120)
        self.publish_road(1, [(0, 400, True, 0)], "全程通行")

    def publish_road(self, version: int, segments, note: str = ""):
        self.svc.publish_road_version(
            request_id=f"road-{version}", actor_id="op", corridor_id="g4", version=version,
            segments=[{"from_km": a, "to_km": b, "open": op, "detour_extra_km": d}
                      for a, b, op, d in segments], note=note)

    def vehicle(self, vehicle_id: str = "truck1", *, priority: str = "normal",
                battery: float = 400, reserve: float = 40,
                empty: float = 1.2, loaded: float = 1.8):
        self.svc.register_vehicle(
            request_id=f"veh-{vehicle_id}", actor_id="op", vehicle_id=vehicle_id,
            display_name=vehicle_id, battery_kwh=battery,
            kwh_per_km_empty=empty, kwh_per_km_loaded=loaded,
            rated_payload_tonnes=30, reserve_kwh=reserve, priority=priority)

    def trip(self, trip_id: str = "trip1", *, vehicle_id: str = "truck1",
             load: float = 30, energy: float = 380, deadline: str = f"{DAY}T18:00Z",
             departure: str = f"{DAY}T06:00Z", origin: float = 0, dest: float = 400,
             priority: str | None = None, driver: str | None = "drv",
             request_id: str | None = None):
        self.vehicle(vehicle_id)
        self.svc.create_trip(
            request_id=request_id or f"trip-{trip_id}", actor_id="op", trip_id=trip_id,
            corridor_id="g4", vehicle_id=vehicle_id, driver_actor_id=driver,
            origin_km=origin, destination_km=dest, load_tonnes=load,
            initial_energy_kwh=energy, deadline=deadline, departure_at=departure,
            priority=priority)

    def plan_and_confirm(self, trip_id: str = "trip1", request_id: str = "plan-1",
                         confirm_id: str = "confirm-1", actor: str = "op"):
        plan = self.svc.plan_energy(request_id=request_id, actor_id=actor, trip_id=trip_id)
        confirmation = self.svc.confirm_plan(request_id=confirm_id, actor_id=actor,
                                             plan_id=plan["plan_id"])
        return plan, confirmation

    def close(self):
        self.database.close()
