# 保障高速服务区充电网络韧性协同基础服务

本项目在综合交通运输共享基础能力（组织、操作者、场所、角色权限、请求幂等、SQLite 事务与哈希串联审计）之上，提供**干线新能源重卡充电保障服务**：接收车辆剩余续航、载重、任务时限、站点设备、分时功率与道路通行版本，为每趟运输生成带有效期并说明余量的补能计划，并在故障、排队超时、提前到达、临时封路等情况下按已完成充电事实重新规划。

## 关键规则

- **带有效期与余量的计划**：每站给出到站预计续航、相对安全余量的差值；计划记录道路版本与各站设备/功率摘要，默认 10 分钟有效，确认时任一项变化即拒绝。
- **多站原子确认**：司机确认在单个 `BEGIN IMMEDIATE` 事务内锁定全部站点的 15 分钟时隙，任一站点容量不足则整体回滚，不留下幽灵占用。
- **双重容量约束**：每个时隙同时校验在运充电桩数量与站点分时功率总额度；检修/故障桩自动剔除。
- **幂等不重复扣减**：确认、事件上报、重规划等写接口均以 `request_id` 去重，重试只回放首次回执。
- **事实驱动改派**：到站、开始充电、累计充电量、故障、排队超时都登记为不可变事实；重规划锚点（位置、时间、真实电量）只从事实推算。
- **优先权边界**：抢险车辆只能挤掉尚未到站的普通预约；已经开始（到站/充电中）的会话受保护，其他抢险预约也不被挤。
- **运营接口**：判断哪些在途车辆仍可安全抵达下一预约站、查询每次改派原因链、查询每个站点各时隙的真实可用功率。
- **重启有效**：预约、事实、计划与审计链全部持久化在 SQLite，进程重启后未到站预约继续有效。

## 目录

- `src/transport_coordination/`
  - `storage.py`：连接、建表与事务边界（含时隙容量台账、事实、计划、预约、改派表）；
  - `charging_planning.py`：通行版本最短路、续航仿真与排队窗口搜索（纯函数）；
  - `charging_admin.py`：节点、站点、充电桩、分时功率、道路段与通行版本登记；
  - `charging_service.py`：行程登记、计划生成、原子确认、抢占、事实上报、重规划、超时扫描、运营查询；
  - `api.py`：stdlib HTTP/JSON 边界；
  - `acceptance.py` / `charging_acceptance.py`：离线端到端验收。
- `tests/`：基础规则、容量与抢占、事实改派、接口路由、重启持久化和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
PYTHONPATH=src python3 -m transport_coordination.charging_acceptance
```

充电保障验收会在临时 SQLite 数据库中构建干线网络（含桩检修、分时降功率），完成计划生成、多站确认与幂等重试、故障事实改派、临时封路检测和重启核对，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

写入接口通过 `X-Actor-Id` 标识操作者，请求体携带 `request_id` 保证幂等。主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/charging-nodes` `/charging-stations` `/chargers` `/power-schedules` | 网络与设备资料 |
| POST | `/road-segments` `/road-versions` `/road-segment-status` | 道路拓扑与通行版本（封路/解封） |
| POST | `/trips` | 登记运输任务（电量、载重、时限、电耗、优先权） |
| POST | `/trips/{id}/plans` | 生成带有效期与余量的补能计划 |
| POST | `/plans/{id}/confirm` | 司机确认，原子锁定多站时隙 |
| POST | `/trips/{id}/events` | 上报到站/充电中/累计电量/完成/故障等事实 |
| POST | `/trips/{id}/replan` | 按事实与最新道路版本重规划 |
| POST | `/queue-timeout-sweep` | 扫描排队超时，释放容量并置待重规划 |
| GET | `/ops/safe-arrivals` | 在途车辆能否安全抵达下一站 |
| GET | `/stations/{id}/capacity` | 各 15 分钟时隙真实可用功率 |
| GET | `/trips/{id}/replans` `/trips/{id}/overview` | 改派原因链与任务全景 |

服务重启后 SQLite 中的业务状态和审计历史继续保留。
