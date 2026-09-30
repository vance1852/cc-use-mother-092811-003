"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
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
-- 干线道路网络与通行版本
CREATE TABLE IF NOT EXISTS charging_nodes (
    node_id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL CHECK(node_type IN ('station', 'waypoint')),
    station_id TEXT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS road_versions (
    version INTEGER PRIMARY KEY,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS road_segments (
    segment_id TEXT PRIMARY KEY,
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    distance_km REAL NOT NULL CHECK(distance_km > 0),
    speed_kmh REAL NOT NULL CHECK(speed_kmh > 0),
    bidirectional INTEGER NOT NULL CHECK(bidirectional IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS road_segment_status (
    segment_id TEXT NOT NULL REFERENCES road_segments(segment_id),
    version INTEGER NOT NULL,
    open INTEGER NOT NULL CHECK(open IN (0, 1)),
    PRIMARY KEY (segment_id, version)
);
-- 站点设备与分时功率
CREATE TABLE IF NOT EXISTS charging_stations (
    station_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL UNIQUE,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    queue_timeout_minutes INTEGER NOT NULL CHECK(queue_timeout_minutes >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chargers (
    charger_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES charging_stations(station_id),
    max_power_kw REAL NOT NULL CHECK(max_power_kw > 0),
    status TEXT NOT NULL CHECK(status IN ('operational', 'maintenance', 'faulted')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS power_schedules (
    schedule_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES charging_stations(station_id),
    effective_date TEXT,
    start_minute INTEGER NOT NULL CHECK(start_minute >= 0 AND start_minute < 1440),
    end_minute INTEGER NOT NULL CHECK(end_minute > 0 AND end_minute <= 1440),
    max_power_kw REAL NOT NULL CHECK(max_power_kw >= 0),
    created_at TEXT NOT NULL
);
-- 运输任务、补能计划与预约
CREATE TABLE IF NOT EXISTS trips (
    trip_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    priority TEXT NOT NULL CHECK(priority IN ('normal', 'rescue')),
    battery_kwh REAL NOT NULL CHECK(battery_kwh > 0),
    initial_energy_kwh REAL NOT NULL CHECK(initial_energy_kwh >= 0),
    depart_at TEXT NOT NULL,
    max_charge_kw REAL NOT NULL CHECK(max_charge_kw > 0),
    load_tons REAL NOT NULL CHECK(load_tons >= 0),
    rate_kwh_per_km REAL NOT NULL CHECK(rate_kwh_per_km > 0),
    origin_node TEXT NOT NULL,
    destination_node TEXT NOT NULL,
    arrive_by TEXT NOT NULL,
    reserve_km REAL NOT NULL CHECK(reserve_km >= 0),
    road_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned', 'confirmed', 'in_progress',
                                         'awaiting_replan', 'completed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trip_state_facts (
    fact_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id TEXT NOT NULL UNIQUE,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    kind TEXT NOT NULL,
    node_id TEXT,
    station_id TEXT,
    charger_id TEXT,
    energy_kwh REAL,
    delivered_kwh REAL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    version_seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'confirmed', 'superseded')),
    feasible INTEGER NOT NULL CHECK(feasible IN (0, 1)),
    road_version INTEGER NOT NULL,
    equipment_digest TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(trip_id, version_seq)
);
CREATE TABLE IF NOT EXISTS plan_stops (
    stop_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    seq INTEGER NOT NULL,
    station_id TEXT NOT NULL,
    arrive_at TEXT NOT NULL,
    depart_at TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    charge_kwh REAL NOT NULL,
    planned_power_kw REAL NOT NULL,
    arrive_range_km REAL NOT NULL,
    reserve_margin_km REAL NOT NULL,
    UNIQUE(plan_id, seq)
);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    plan_id TEXT NOT NULL UNIQUE REFERENCES plans(plan_id),
    status TEXT NOT NULL CHECK(status IN ('active', 'superseded')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservation_stops (
    res_stop_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    plan_stop_id TEXT REFERENCES plan_stops(stop_id),
    seq INTEGER NOT NULL,
    station_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('reserved', 'arrived', 'charging', 'completed',
                                         'departed', 'faulted', 'cancelled', 'displaced')),
    is_fact INTEGER NOT NULL DEFAULT 0 CHECK(is_fact IN (0, 1)),
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    arrive_at TEXT,
    depart_at TEXT,
    charge_kwh REAL NOT NULL,
    planned_power_kw REAL NOT NULL,
    delivered_kwh REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reservation_slot_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    res_stop_id TEXT NOT NULL REFERENCES reservation_stops(res_stop_id),
    reservation_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    station_id TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    overlap_minutes INTEGER NOT NULL DEFAULT 15 CHECK(overlap_minutes > 0 AND overlap_minutes <= 15),
    power_kw REAL NOT NULL CHECK(power_kw >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    UNIQUE(res_stop_id, slot_start)
);
CREATE INDEX IF NOT EXISTS idx_slot_capacity
    ON reservation_slot_ledger(station_id, slot_start, active);
CREATE TABLE IF NOT EXISTS replans (
    replan_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    seq INTEGER NOT NULL,
    reason TEXT NOT NULL,
    from_plan_id TEXT,
    to_plan_id TEXT,
    detail_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(trip_id, seq)
);
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

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
