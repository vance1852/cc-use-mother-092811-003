"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    display_name TEXT NOT NULL,
    battery_kwh REAL NOT NULL CHECK(battery_kwh > 0),
    kwh_per_km_empty REAL NOT NULL CHECK(kwh_per_km_empty > 0),
    kwh_per_km_loaded REAL NOT NULL CHECK(kwh_per_km_loaded >= kwh_per_km_empty),
    rated_payload_tonnes REAL NOT NULL CHECK(rated_payload_tonnes > 0),
    reserve_kwh REAL NOT NULL CHECK(reserve_kwh >= 0),
    priority TEXT NOT NULL CHECK(priority IN ('normal', 'rescue')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corridors (
    corridor_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    display_name TEXT NOT NULL,
    length_km REAL NOT NULL CHECK(length_km > 0),
    avg_speed_kmh REAL NOT NULL CHECK(avg_speed_kmh > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charging_stations (
    station_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL REFERENCES corridors(corridor_id),
    display_name TEXT NOT NULL,
    position_km REAL NOT NULL CHECK(position_km >= 0),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL,
    UNIQUE(corridor_id, position_km)
);
CREATE TABLE IF NOT EXISTS chargers (
    charger_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES charging_stations(station_id),
    rated_power_kw REAL NOT NULL CHECK(rated_power_kw > 0),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS station_outages (
    outage_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES charging_stations(station_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charger_outages (
    outage_id TEXT PRIMARY KEY,
    charger_id TEXT NOT NULL REFERENCES chargers(charger_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS power_windows (
    window_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES charging_stations(station_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    cap_kw REAL NOT NULL CHECK(cap_kw >= 0),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS road_versions (
    corridor_id TEXT NOT NULL REFERENCES corridors(corridor_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    note TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY(corridor_id, version)
);
CREATE TABLE IF NOT EXISTS road_segments (
    corridor_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    from_km REAL NOT NULL,
    to_km REAL NOT NULL,
    open INTEGER NOT NULL CHECK(open IN (0, 1)),
    detour_extra_km REAL NOT NULL CHECK(detour_extra_km >= 0),
    note TEXT NOT NULL,
    PRIMARY KEY(corridor_id, version, from_km, to_km)
);
CREATE TABLE IF NOT EXISTS trips (
    trip_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL REFERENCES corridors(corridor_id),
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    driver_actor_id TEXT REFERENCES actors(actor_id),
    origin_km REAL NOT NULL,
    destination_km REAL NOT NULL,
    load_tonnes REAL NOT NULL CHECK(load_tonnes >= 0),
    initial_energy_kwh REAL NOT NULL,
    deadline TEXT NOT NULL,
    departure_at TEXT NOT NULL,
    road_version INTEGER NOT NULL,
    priority TEXT NOT NULL CHECK(priority IN ('normal', 'rescue')),
    state TEXT NOT NULL CHECK(state IN ('planned', 'en_route', 'completed', 'abandoned')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_plans (
    plan_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    plan_no INTEGER NOT NULL,
    road_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'confirmed', 'superseded', 'expired')),
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    anchor_km REAL NOT NULL,
    anchor_at TEXT NOT NULL,
    anchor_energy_kwh REAL NOT NULL,
    feasible INTEGER NOT NULL CHECK(feasible IN (0, 1)),
    supersede_reason TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(trip_id, plan_no)
);
CREATE TABLE IF NOT EXISTS energy_plan_legs (
    plan_id TEXT NOT NULL REFERENCES energy_plans(plan_id),
    seq INTEGER NOT NULL,
    station_id TEXT NOT NULL REFERENCES charging_stations(station_id),
    arrive_at TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    power_kw REAL NOT NULL,
    charge_kwh REAL NOT NULL,
    arrive_energy_kwh REAL NOT NULL,
    leave_energy_kwh REAL NOT NULL,
    PRIMARY KEY(plan_id, seq)
);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE REFERENCES energy_plans(plan_id),
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    status TEXT NOT NULL CHECK(status IN ('active', 'completed', 'superseded', 'displaced_by_rescue')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservation_legs (
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    seq INTEGER NOT NULL,
    station_id TEXT NOT NULL REFERENCES charging_stations(station_id),
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    charger_id TEXT NOT NULL REFERENCES chargers(charger_id),
    power_kw REAL NOT NULL,
    charge_kwh REAL NOT NULL,
    leg_state TEXT NOT NULL CHECK(leg_state IN ('reserved', 'arrived', 'charging',
                                               'completed', 'released', 'failed', 'faulted')),
    actual_arrival TEXT,
    started_at TEXT,
    completed_at TEXT,
    delivered_kwh REAL,
    PRIMARY KEY(reservation_id, seq)
);
CREATE TABLE IF NOT EXISTS slot_occupancy (
    station_id TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    charger_id TEXT NOT NULL,
    draw_kw REAL NOT NULL,
    occupancy_state TEXT NOT NULL CHECK(occupancy_state IN ('reserved', 'arrived', 'charging')),
    PRIMARY KEY(station_id, slot_start, reservation_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_occupancy_charger_slot
ON slot_occupancy(station_id, charger_id, slot_start)
WHERE occupancy_state IN ('reserved', 'arrived', 'charging');
CREATE INDEX IF NOT EXISTS idx_occupancy_station_slot
ON slot_occupancy(station_id, slot_start);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_reservation_trip
ON reservations(trip_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS charging_sessions (
    session_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    station_id TEXT NOT NULL REFERENCES charging_stations(station_id),
    charger_id TEXT NOT NULL REFERENCES chargers(charger_id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    delivered_kwh REAL NOT NULL DEFAULT 0,
    session_state TEXT NOT NULL CHECK(session_state IN ('charging', 'completed', 'faulted')),
    UNIQUE(reservation_id, seq)
);
CREATE TABLE IF NOT EXISTS trip_events (
    event_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    kind TEXT NOT NULL,
    at_km REAL,
    from_plan_id TEXT,
    to_plan_id TEXT,
    detail_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trip_events_trip ON trip_events(trip_id, occurred_at);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        # 多个 HTTP 工作线程共享同一连接，写事务必须串行，避免 BEGIN/COMMIT 交错
        self._write_lock = threading.Lock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；写事务之间串行执行。"""

        self._write_lock.acquire()
        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            self._write_lock.release()
            raise
        else:
            self.connection.commit()
            self._write_lock.release()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
