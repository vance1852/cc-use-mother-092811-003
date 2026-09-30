"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .charging_service import ChargingService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def jsonable(value: Any) -> Any:
    """把数据对象、元组等递归转换为可 JSON 序列化的结构。"""

    if dataclasses.is_dataclass(value):
        return {key: jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    return value


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    path = parsed.path
    try:
        # ------------------------------------------------------- 基础服务
        if method == "GET" and path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        # ------------------------------------------------- 充电网络资料
        if not isinstance(service, ChargingService):
            return 404, {"error": "route_not_found", "message": "接口不存在"}
        if method == "POST" and path == "/charging-nodes":
            receipt = service.register_node(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and path == "/charging-stations":
            receipt = service.register_station(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and path == "/chargers":
            receipt = service.upsert_charger(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and path == "/power-schedules":
            receipt = service.set_power_schedule(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and path == "/road-segments":
            receipt = service.upsert_segment(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and path == "/road-versions":
            receipt = service.create_road_version(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and path == "/road-segment-status":
            receipt = service.set_segment_open(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__

        # ------------------------------------------------------- 运输任务
        if method == "POST" and path == "/trips":
            receipt = service.register_trip(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__

        match = re.fullmatch(r"/trips/([A-Za-z0-9_.:-]+)/plans", path)
        if method == "POST" and match:
            plan = service.generate_plan(actor_id=actor_id, trip_id=match.group(1), **body)
            return 201, jsonable(plan)

        match = re.fullmatch(r"/trips/([A-Za-z0-9_.:-]+)/events", path)
        if method == "POST" and match:
            receipt = service.report_event(actor_id=actor_id, trip_id=match.group(1), **body)
            return 200 if receipt.replayed else 201, receipt.__dict__

        match = re.fullmatch(r"/trips/([A-Za-z0-9_.:-]+)/replan", path)
        if method == "POST" and match:
            plan = service.replan(actor_id=actor_id, trip_id=match.group(1), **body)
            return 201, jsonable(plan)

        match = re.fullmatch(r"/trips/([A-Za-z0-9_.:-]+)/replans", path)
        if method == "GET" and match:
            return 200, {"items": jsonable(service.list_replans(match.group(1)))}

        match = re.fullmatch(r"/trips/([A-Za-z0-9_.:-]+)/overview", path)
        if method == "GET" and match:
            return 200, jsonable(service.trip_overview(match.group(1)))

        match = re.fullmatch(r"/trips/([A-Za-z0-9_.:-]+)/reservation", path)
        if method == "GET" and match:
            reservation = service.get_reservation(match.group(1))
            if reservation is None:
                return 404, {"error": "not_found", "message": "当前没有生效预约"}
            return 200, jsonable(reservation)

        if method == "POST" and path == "/queue-timeout-sweep":
            return 200, {"items": service.sweep_queue_timeouts(**body)}

        # ------------------------------------------------------- 计划确认
        match = re.fullmatch(r"/plans/([A-Za-z0-9_.:-]+)/confirm", path)
        if method == "POST" and match:
            receipt, reservation = service.confirm_plan(
                actor_id=actor_id, plan_id=match.group(1), **body)
            return 200 if receipt.replayed else 201, \
                {"receipt": receipt.__dict__, "reservation": jsonable(reservation)}

        match = re.fullmatch(r"/plans/([A-Za-z0-9_.:-]+)", path)
        if method == "GET" and match:
            return 200, jsonable(service.get_plan(match.group(1)))

        # ------------------------------------------------------- 运营查询
        if method == "GET" and path == "/ops/safe-arrivals":
            return 200, {"items": service.safe_arrivals()}

        match = re.fullmatch(r"/stations/([A-Za-z0-9_.:-]+)/capacity", path)
        if method == "GET" and match:
            return 200, service.station_capacity(
                match.group(1),
                from_at=query.get("from_at", [None])[0],
                slots=int(query.get("slots", ["16"])[0]))

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动干线充电保障服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = ChargingService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
