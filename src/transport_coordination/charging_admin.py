"""管理干线节点、道路通行版本、站点设备与分时功率。"""

from __future__ import annotations

import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .charging_models import RoadNetwork, StationEquipment
from .errors import NotFoundError, ValidationError
from .service import DomainService


class ChargingAdminService(DomainService):
    """登记充电网络静态资料与道路通行版本。"""

    # ------------------------------------------------------------------ 节点

    def register_node(self, *, request_id: str, actor_id: str, node_id: str, name: str,
                      node_type: str = "waypoint", station_id: str | None = None) -> Any:
        payload = {"node_id": node_id, "name": name, "node_type": node_type, "station_id": station_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            node_id = self._identifier(node_id, "node_id")
            name = self._text(name, "name")
            if node_type not in {"station", "waypoint"}:
                raise ValidationError("node_type 必须是 station 或 waypoint")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO charging_nodes(node_id,node_type,station_id,name,created_at) VALUES(?,?,?,?,?)",
                    (node_id, node_type, station_id, name, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="charging_node.registered",
                             resource_type="charging_node", resource_id=node_id,
                             detail={"name": name, "node_type": node_type, "station_id": station_id},
                             occurred_at=self._now())
                return "charging_node", node_id, {"node_id": node_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_charging_node", payload=payload, create=create)

    # ------------------------------------------------------------------ 站点

    def register_station(self, *, request_id: str, actor_id: str, station_id: str,
                         organization_id: str, name: str, node_id: str,
                         queue_timeout_minutes: int = 30) -> Any:
        payload = {"station_id": station_id, "organization_id": organization_id, "name": name,
                   "node_id": node_id, "queue_timeout_minutes": queue_timeout_minutes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            station_id = self._identifier(station_id, "station_id")
            node_id = self._identifier(node_id, "node_id")
            name = self._text(name, "name")
            timeout = int(queue_timeout_minutes)
            if timeout < 0:
                raise ValidationError("queue_timeout_minutes 不能为负")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM charging_nodes WHERE node_id=?", (node_id,)).fetchone() is None:
                    raise NotFoundError("道路节点不存在，请先登记节点")
                connection.execute(
                    "INSERT INTO charging_nodes(node_id,node_type,station_id,name,created_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET node_type='station', station_id=?",
                    (node_id, "station", station_id, name, self._now(), station_id),
                )
                try:
                    connection.execute(
                        "INSERT INTO charging_stations(station_id,node_id,organization_id,name,"
                        "queue_timeout_minutes,created_at) VALUES(?,?,?,?,?,?)",
                        (station_id, node_id, organization_id, name, timeout, self._now()),
                    )
                except Exception as exc:
                    raise ValidationError("站点编号重复、节点已被占用或组织不存在") from exc
                append_event(connection, actor_id=actor_id, action="charging_station.registered",
                             resource_type="charging_station", resource_id=station_id,
                             detail={"node_id": node_id, "name": name, "queue_timeout_minutes": timeout},
                             occurred_at=self._now())
                return "charging_station", station_id, {"station_id": station_id, "node_id": node_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_charging_station", payload=payload, create=create)

    # ------------------------------------------------------------------ 设备

    def upsert_charger(self, *, request_id: str, actor_id: str, charger_id: str,
                       station_id: str, max_power_kw: float, status: str = "operational") -> Any:
        payload = {"charger_id": charger_id, "station_id": station_id,
                   "max_power_kw": max_power_kw, "status": status}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            charger_id = self._identifier(charger_id, "charger_id")
            station_id = self._identifier(station_id, "station_id")
            power = float(max_power_kw)
            if power <= 0:
                raise ValidationError("max_power_kw 必须为正数")
            if status not in {"operational", "maintenance", "faulted"}:
                raise ValidationError("充电桩状态不合法")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM charging_stations WHERE station_id=?",
                                      (station_id,)).fetchone() is None:
                    raise NotFoundError("站点不存在")
                existing = connection.execute("SELECT status FROM chargers WHERE charger_id=?",
                                              (charger_id,)).fetchone()
                connection.execute(
                    "INSERT INTO chargers(charger_id,station_id,max_power_kw,status,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(charger_id) DO UPDATE SET station_id=excluded.station_id,"
                    "max_power_kw=excluded.max_power_kw,status=excluded.status,updated_at=excluded.updated_at",
                    (charger_id, station_id, power, status, self._now()),
                )
                append_event(connection, actor_id=actor_id,
                             action="charger.upserted" if existing is None else "charger.updated",
                             resource_type="charger", resource_id=charger_id,
                             detail={"station_id": station_id, "max_power_kw": power,
                                     "old_status": existing["status"] if existing else None,
                                     "new_status": status},
                             occurred_at=self._now())
                return "charger", charger_id, {"charger_id": charger_id, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="upsert_charger", payload=payload, create=create)

    def set_power_schedule(self, *, request_id: str, actor_id: str, station_id: str,
                           windows: list[dict[str, Any]], effective_date: str | None = None) -> Any:
        normalized = []
        for window in windows:
            start = int(window["start_minute"])
            end = int(window["end_minute"])
            power = float(window["max_power_kw"])
            if not (0 <= start < end <= 1440):
                raise ValidationError("功率时段必须满足 0<=start<end<=1440")
            if power < 0:
                raise ValidationError("功率限额不能为负")
            normalized.append((start, end, power))
        normalized.sort()
        if not normalized or normalized[0][0] != 0 or normalized[-1][1] != 1440:
            raise ValidationError("功率时段必须完整覆盖 0 到 1440 分钟")
        for (_, previous_end, _), (start, _, _) in zip(normalized, normalized[1:]):
            if previous_end != start:
                raise ValidationError("功率时段必须首尾相接、不得重叠或留空档")
        payload = {"station_id": station_id, "windows": normalized, "effective_date": effective_date}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            station_id = self._identifier(station_id, "station_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM charging_stations WHERE station_id=?",
                                      (station_id,)).fetchone() is None:
                    raise NotFoundError("站点不存在")
                connection.execute(
                    "DELETE FROM power_schedules WHERE station_id=? AND IFNULL(effective_date,'')=IFNULL(?,'')",
                    (station_id, effective_date),
                )
                for start, end, power in normalized:
                    connection.execute(
                        "INSERT INTO power_schedules(schedule_id,station_id,effective_date,start_minute,"
                        "end_minute,max_power_kw,created_at) VALUES(?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, station_id, effective_date, start, end, power, self._now()),
                    )
                append_event(connection, actor_id=actor_id, action="power_schedule.set",
                             resource_type="charging_station", resource_id=station_id,
                             detail={"effective_date": effective_date, "windows": normalized},
                             occurred_at=self._now())
                return "power_schedule", station_id, {"station_id": station_id, "windows": len(normalized)}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_power_schedule", payload=payload, create=create)

    # ------------------------------------------------------------------ 道路

    def upsert_segment(self, *, request_id: str, actor_id: str, segment_id: str,
                       from_node: str, to_node: str, distance_km: float, speed_kmh: float,
                       bidirectional: bool = True) -> Any:
        payload = {"segment_id": segment_id, "from_node": from_node, "to_node": to_node,
                   "distance_km": distance_km, "speed_kmh": speed_kmh,
                   "bidirectional": bidirectional}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            segment_id = self._identifier(segment_id, "segment_id")
            distance = float(distance_km)
            speed = float(speed_kmh)
            if distance <= 0 or speed <= 0:
                raise ValidationError("距离和时速必须为正数")

            def create() -> tuple[str, str, dict[str, Any]]:
                for node in (from_node, to_node):
                    if connection.execute("SELECT 1 FROM charging_nodes WHERE node_id=?", (node,)).fetchone() is None:
                        raise NotFoundError(f"道路节点 {node} 不存在")
                connection.execute(
                    "INSERT INTO road_segments(segment_id,from_node,to_node,distance_km,speed_kmh,"
                    "bidirectional,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(segment_id) DO UPDATE SET "
                    "from_node=excluded.from_node,to_node=excluded.to_node,distance_km=excluded.distance_km,"
                    "speed_kmh=excluded.speed_kmh,bidirectional=excluded.bidirectional",
                    (segment_id, from_node, to_node, distance, speed, 1 if bidirectional else 0, self._now()),
                )
                # 新段在所有既有版本默认开通；尚未登记状态的版本补齐为开通
                connection.execute(
                    "INSERT OR IGNORE INTO road_segment_status(segment_id,version,open) "
                    "SELECT ?, version, 1 FROM road_versions",
                    (segment_id,),
                )
                append_event(connection, actor_id=actor_id, action="road_segment.upserted",
                             resource_type="road_segment", resource_id=segment_id,
                             detail={"from_node": from_node, "to_node": to_node,
                                     "distance_km": distance, "speed_kmh": speed,
                                     "bidirectional": bidirectional},
                             occurred_at=self._now())
                return "road_segment", segment_id, {"segment_id": segment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="upsert_road_segment", payload=payload, create=create)

    def create_road_version(self, *, request_id: str, actor_id: str, note: str = "",
                            closures: list[str] | None = None,
                            openings: list[str] | None = None) -> Any:
        payload = {"note": note, "closures": sorted(closures or []), "openings": sorted(openings or [])}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute("SELECT MAX(version) AS version FROM road_versions").fetchone()
                version = (row["version"] or 0) + 1
                connection.execute(
                    "INSERT INTO road_versions(version,note,created_at) VALUES(?,?,?)",
                    (version, note, self._now()),
                )
                connection.execute(
                    "INSERT INTO road_segment_status(segment_id,version,open) "
                    "SELECT segment_id, ?, open FROM road_segment_status WHERE version=?",
                    (version, version - 1),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO road_segment_status(segment_id,version,open) "
                    "SELECT segment_id, ?, 1 FROM road_segments",
                    (version,),
                )
                for segment_id in closures or []:
                    self._flip_segment(connection, segment_id, version, open_flag=0)
                for segment_id in openings or []:
                    self._flip_segment(connection, segment_id, version, open_flag=1)
                append_event(connection, actor_id=actor_id, action="road_version.created",
                             resource_type="road_version", resource_id=str(version),
                             detail={"note": note, "closures": closures or [], "openings": openings or []},
                             occurred_at=self._now())
                return "road_version", str(version), {"version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_road_version", payload=payload, create=create)

    def _flip_segment(self, connection, segment_id: str, version: int, *, open_flag: int) -> None:
        if connection.execute("SELECT 1 FROM road_segments WHERE segment_id=?", (segment_id,)).fetchone() is None:
            raise NotFoundError(f"道路段 {segment_id} 不存在")
        connection.execute(
            "INSERT INTO road_segment_status(segment_id,version,open) VALUES(?,?,?) "
            "ON CONFLICT(segment_id,version) DO UPDATE SET open=excluded.open",
            (segment_id, version, open_flag),
        )

    def set_segment_open(self, *, request_id: str, actor_id: str, segment_id: str,
                         version: int, open: bool) -> Any:
        payload = {"segment_id": segment_id, "version": version, "open": open}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM road_versions WHERE version=?",
                                      (version,)).fetchone() is None:
                    raise NotFoundError("道路版本不存在")
                self._flip_segment(connection, segment_id, version, open_flag=1 if open else 0)
                append_event(connection, actor_id=actor_id,
                             action="road_segment.opened" if open else "road_segment.closed",
                             resource_type="road_segment", resource_id=segment_id,
                             detail={"version": version}, occurred_at=self._now())
                return "road_segment_status", f"{segment_id}@{version}", {"open": open}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_segment_open", payload=payload, create=create)

    # ------------------------------------------------------------------ 快照

    def get_road_network(self, version: int | None = None) -> RoadNetwork:
        connection = self.database.connection
        if version is None:
            row = connection.execute("SELECT MAX(version) AS version FROM road_versions").fetchone()
            if row["version"] is None:
                raise NotFoundError("尚未发布任何道路通行版本")
            version = row["version"]
        header = connection.execute("SELECT * FROM road_versions WHERE version=?", (version,)).fetchone()
        if header is None:
            raise NotFoundError("道路版本不存在")
        nodes = {row["node_id"]: {"node_id": row["node_id"], "node_type": row["node_type"],
                                  "station_id": row["station_id"], "name": row["name"]}
                 for row in connection.execute("SELECT * FROM charging_nodes")}
        segments: dict[str, dict[str, Any]] = {}
        open_ids: set[str] = set()
        for seg in connection.execute("SELECT * FROM road_segments"):
            status = connection.execute(
                "SELECT open FROM road_segment_status WHERE segment_id=? AND version=?",
                (seg["segment_id"], version),
            ).fetchone()
            is_open = bool(status["open"]) if status is not None else True
            segments[seg["segment_id"]] = {
                "segment_id": seg["segment_id"], "from_node": seg["from_node"], "to_node": seg["to_node"],
                "distance_km": seg["distance_km"], "speed_kmh": seg["speed_kmh"],
                "bidirectional": bool(seg["bidirectional"]), "open": is_open,
            }
            if is_open:
                open_ids.add(seg["segment_id"])
        return RoadNetwork(version=version, note=header["note"], nodes=nodes, segments=segments,
                           open_segment_ids=frozenset(open_ids), generated_at=self._now())

    def get_station_equipment(self, station_id: str) -> StationEquipment:
        connection = self.database.connection
        station = connection.execute("SELECT * FROM charging_stations WHERE station_id=?",
                                     (station_id,)).fetchone()
        if station is None:
            raise NotFoundError("站点不存在")
        chargers = tuple({"charger_id": row["charger_id"], "max_power_kw": row["max_power_kw"],
                          "status": row["status"]}
                         for row in connection.execute(
                             "SELECT * FROM chargers WHERE station_id=? ORDER BY charger_id", (station_id,)))
        windows = tuple({"start_minute": row["start_minute"], "end_minute": row["end_minute"],
                         "max_power_kw": row["max_power_kw"], "effective_date": row["effective_date"]}
                        for row in connection.execute(
                            "SELECT * FROM power_schedules WHERE station_id=? AND effective_date IS NULL "
                            "ORDER BY start_minute", (station_id,)))
        material = {"station_id": station_id,
                    "chargers": sorted((c["charger_id"], c["max_power_kw"], c["status"]) for c in chargers),
                    "windows": sorted((w["start_minute"], w["end_minute"], w["max_power_kw"]) for w in windows)}
        return StationEquipment(station_id=station_id, name=station["name"],
                                queue_timeout_minutes=station["queue_timeout_minutes"],
                                chargers=chargers, power_schedule=windows,
                                digest=digest(material), generated_at=self._now())

    def station_power_limit(self, station_id: str, slot_start_minute: int) -> float:
        """返回某 15 分钟时隙内适用的分时功率上限（取覆盖窗口最小值）。"""

        equipment = self.get_station_equipment(station_id)
        applicable = [w["max_power_kw"] for w in equipment.power_schedule
                      if w["start_minute"] < slot_start_minute + 15 and w["end_minute"] > slot_start_minute]
        return min(applicable) if applicable else float("inf")
