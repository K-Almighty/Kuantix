"""monitor 路由（契约 §2.4 M1–M17，前缀 ``/api/v1/monitor``）。

端点：
- M1/M2/M3 —— start / stop / status（经 :class:`MonitorLoop`）；
- M4/M5/M6 —— watchlist 查询 / 批量新增 / 删除；
- M7 —— 判据插件清单（``RuleEngine.criteria_info()``）；
- M8/M9/M10/M11 —— 规则 CRUD（判据/级别非法 → 400，不存在 → 404）；
- M12/M13/M14 —— 持仓盈亏视图 / 新增 / 删除；
- M15 —— 告警历史（分页 + level 过滤）；
- M16 —— 推送通道状态（``Notifier.channels_info()``）；
- M17 —— WebSocket：hello → snapshot → 实时 alert（EVENT_BUS TOPIC_ALERT
  订阅转发，**每客户端独立订阅/退订**）→ ping/pong 心跳 → bye。

服务层经组合根（``ServiceContainer.monitor*``）获取，**不直接 import
easy_tdx**（R2）。比例口径照契约：``change_pct``/``pnl_pct`` 小数（0.05=5%）。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import Response

from Kuantix import __version__
from Kuantix.api.deps import (
    ServiceContainer,
    db_page,
    flat_position_view,
    get_services_from_app,
    page_limits,
    paginate,
    resolve_market,
    respond,
)
from Kuantix.api.schemas import PositionInput, RuleInput, WatchlistAddRequest
from Kuantix.core.contracts import Position
from Kuantix.core.envelope import Envelope, Timer
from Kuantix.core.eventbus import EVENT_BUS, TOPIC_ALERT
from Kuantix.core.fail_loud import DataIntegrityError, MissingKeyError, UnknownValueError
from Kuantix.core.market import get_market_profile
from Kuantix.monitor.rules import KNOWN_CRITERION_TYPES

__all__ = ["router", "PING_INTERVAL_SECONDS", "CLIENT_TIMEOUT_SECONDS", "WATCHLIST_MAX"]

logger = logging.getLogger(__name__)

router = APIRouter()

#: M17 心跳参数（契约 §2.4.1：服务端每 30s 主动 ping 保活）
PING_INTERVAL_SECONDS = 30.0
#: M17 客户端 60s 无响应关闭
CLIENT_TIMEOUT_SECONDS = 60.0
#: M5 自选批量上限（契约 §2.4 M5 默认 100）
WATCHLIST_MAX = 100
#: M15 单次拉取告警上限（本地工具，足够分页展示）
_ALERTS_FETCH_LIMIT = 10000

#: 告警级别白名单（M15 level 过滤）
_ALERT_LEVELS: tuple[str, ...] = ("info", "warning", "critical")


def _services(request: Request) -> ServiceContainer:
    return get_services_from_app(request.app)


def _require_rule_level(level: str) -> str:
    """校验告警级别；非法 → 400（契约 M9/M10 判据/参数非法）。"""
    value = str(level).strip().lower()
    if value not in _ALERT_LEVELS:
        raise MissingKeyError(
            f"[fail-loud/NF-26] 规则级别非法: {level!r}（期望 {sorted(_ALERT_LEVELS)}）"
        )
    return value


def _require_criterion_type(criterion_type: str) -> str:
    """校验判据类型；非法 → 400（契约 M9）。"""
    value = str(criterion_type).strip()
    if value not in KNOWN_CRITERION_TYPES:
        raise MissingKeyError(
            f"[fail-loud/NF-26] 判据类型非法: {criterion_type!r}"
            f"（期望 {sorted(KNOWN_CRITERION_TYPES)}）"
        )
    return value


#: 各判据的必填参数（契约 §3.5 Rule.params）
_CRITERION_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "price": ("op", "threshold"),
    "indicator": ("indicator", "op"),
    "stop_loss": ("base", "pct"),
}


def _require_params(criterion_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """校验判据参数必填键；缺失 → 400（契约 M9 参数非法）。"""
    required = _CRITERION_REQUIRED_PARAMS[criterion_type]
    missing = [key for key in required if key not in params]
    if missing:
        raise MissingKeyError(
            f"[fail-loud/NF-26] {criterion_type} 判据缺少必填参数 {missing}"
        )
    return dict(params)


def _require_codes(codes: list[str] | None, market: str, context: str) -> tuple[str, ...]:
    """校验规则/自选代码列表；空或格式非法 → 400（契约 M5/M9）。

    代码格式经 :meth:`MarketProfile.exchange_for_code` 判定（NF-5），
    未知代码段按「参数非法」映射为 400（不是数据完整性 422）。
    """
    if not codes:
        raise MissingKeyError(f"[fail-loud/NF-26] {context} 代码列表不能为空")
    profile = get_market_profile(market)
    result: list[str] = []
    for raw in codes:
        code = str(raw).strip()
        if not code:
            raise MissingKeyError(f"[fail-loud/NF-26] {context} 存在空代码")
        try:
            profile.exchange_for_code(code)
        except UnknownValueError as exc:
            raise MissingKeyError(
                f"[fail-loud/NF-26] {context} 代码格式非法: {code!r}"
            ) from exc
        result.append(code)
    return tuple(dict.fromkeys(result))


# ---------------------------------------------------------------------------
# M1-M3 生命周期
# ---------------------------------------------------------------------------


@router.post("/start", summary="启动监控（M1）")
async def monitor_start(request: Request, market: str = "CN") -> Response:
    """启动监控循环（后台线程，不阻塞）。无自选 → 422。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    if not container.monitor.watchlist_codes(code):
        raise DataIntegrityError(
            f"[fail-loud/NF-26] 无自选无法启动监控（{code}），请先添加 watchlist"
        )
    with Timer() as timer:
        payload = container.monitor.start()
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.post("/stop", summary="停止监控（M2）")
async def monitor_stop(request: Request, market: str = "CN") -> Response:
    """优雅停止监控循环。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        payload = container.monitor.stop()
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.get("/status", summary="监控状态（M3）")
async def monitor_status(request: Request, market: str = "CN") -> Response:
    """返回运行状态与轮询健康度。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        payload = container.monitor.status().to_dict()
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


# ---------------------------------------------------------------------------
# M4-M6 自选
# ---------------------------------------------------------------------------


@router.get("/watchlist", summary="自选列表（M4）")
async def monitor_watchlist(
    request: Request,
    market: str = "CN",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Response:
    """分页列出监控自选。P1-2：LIMIT/OFFSET 下推到 SQLite。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        limit, offset = page_limits(page, page_size)
        total = container.monitor.count_watch(code)
        items = [item.to_dict() for item in container.monitor.list_watch(code, limit=limit, offset=offset)]
        payload = db_page(items, total, page, page_size)
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.post("/watchlist", summary="批量新增自选（M5）")
async def monitor_watchlist_add(
    request: Request, body: WatchlistAddRequest
) -> Response:
    """批量新增自选；代码非法 → 400，超上限 → 422。"""
    container = _services(request)
    code = resolve_market(container.config, body.market)
    codes = _require_codes(body.codes, code, "watchlist")
    if len(codes) > WATCHLIST_MAX:
        raise DataIntegrityError(
            f"[fail-loud/NF-26] 单次新增自选 {len(codes)} 条超过上限 {WATCHLIST_MAX}"
        )
    source = str(body.source or "manual").strip() or "manual"
    with Timer() as timer:
        current = set(container.monitor.watchlist_codes(code))
        added: list[str] = []
        skipped: list[dict[str, Any]] = []
        for c in codes:
            if len(current) + len(added) >= WATCHLIST_MAX:
                skipped.append({"code": c, "reason": "watchlist_full"})
                continue
            container.monitor.add_watch(c, market=code, source=source)
            added.append(c)
        payload = {"added": added, "skipped": skipped}
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.delete("/watchlist/{code}", summary="删除自选（M6）")
async def monitor_watchlist_remove(
    request: Request, code: str, market: str = "CN"
) -> Response:
    """删除自选；不存在 → 404。"""
    container = _services(request)
    market_code = resolve_market(container.config, market)
    with Timer() as timer:
        removed = container.monitor.remove_watch(code)
    if not removed:
        raise StarletteHTTPException(
            status_code=404, detail=f"自选不存在: {code}"
        )
    return respond({"removed": code}, market_code, elapsed_ms=timer.elapsed_ms)


# ---------------------------------------------------------------------------
# M7 判据清单
# ---------------------------------------------------------------------------


@router.get("/criteria", summary="判据插件清单（M7）")
async def monitor_criteria(request: Request) -> Response:
    """列出价格/指标/止损判据插件（NF-2）。"""
    container = _services(request)
    with Timer() as timer:
        payload = {"items": container.monitor_engine.criteria_info()}
    return respond(payload, "CN", elapsed_ms=timer.elapsed_ms)


# ---------------------------------------------------------------------------
# M8-M11 规则
# ---------------------------------------------------------------------------


@router.get("/rules", summary="规则列表（M8）")
async def monitor_rules(
    request: Request,
    market: str = "CN",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Response:
    """分页列出预警规则。P1-2：LIMIT/OFFSET 下推到 SQLite。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        limit, offset = page_limits(page, page_size)
        total = container.monitor.count_rules(code)
        items = [rule.to_dict() for rule in container.monitor.list_rules(code, limit=limit, offset=offset)]
        payload = db_page(items, total, page, page_size)
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.post("/rules", summary="新增规则（M9）")
async def monitor_rules_add(request: Request, body: RuleInput) -> Response:
    """新增规则，返回含生成 id 的 Rule。判据/级别非法 → 400。"""
    container = _services(request)
    code = resolve_market(container.config, body.market or "CN")
    if not str(body.name or "").strip():
        raise MissingKeyError("[fail-loud/NF-26] 规则名 name 不能为空")
    criterion_type = _require_criterion_type(str(body.criterion_type or ""))
    level = _require_rule_level(str(body.level or ""))
    codes = _require_codes(body.codes, code, "rule")
    params = _require_params(criterion_type, body.params)
    cooldown = (
        float(body.cooldown_seconds)
        if body.cooldown_seconds is not None
        else 300.0
    )
    enabled = bool(body.enabled) if body.enabled is not None else True
    with Timer() as timer:
        rule = container.monitor_engine.create_rule(
            name=str(body.name).strip(),
            market=code,
            codes=codes,
            criterion_type=criterion_type,
            params=params,
            level=level,
            cooldown_seconds=cooldown,
            enabled=enabled,
        )
    return respond(rule.to_dict(), code, elapsed_ms=timer.elapsed_ms)


@router.put("/rules/{rule_id}", summary="更新规则（M10）")
async def monitor_rules_update(
    request: Request, rule_id: str, body: RuleInput
) -> Response:
    """部分更新规则；不存在 → 404。"""
    container = _services(request)
    current = container.monitor.get_rule(rule_id)
    if current is None:
        raise StarletteHTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.market is not None:
        fields["market"] = resolve_market(container.config, body.market)
    if body.codes is not None:
        fields["codes"] = body.codes
    if body.criterion_type is not None:
        fields["criterion_type"] = _require_criterion_type(body.criterion_type)
    if body.params:
        fields["params"] = body.params
    if body.level is not None:
        fields["level"] = _require_rule_level(body.level)
    if body.cooldown_seconds is not None:
        fields["cooldown_seconds"] = body.cooldown_seconds
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    with Timer() as timer:
        updated = container.monitor_engine.update_rule(rule_id, **fields)
    return respond(updated.to_dict(), updated.market, elapsed_ms=timer.elapsed_ms)


@router.delete("/rules/{rule_id}", summary="删除规则（M11）")
async def monitor_rules_remove(request: Request, rule_id: str) -> Response:
    """删除规则；不存在 → 404。"""
    container = _services(request)
    with Timer() as timer:
        removed = container.monitor.delete_rule(rule_id)
    if not removed:
        raise StarletteHTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return respond({"removed": rule_id}, "CN", elapsed_ms=timer.elapsed_ms)


# ---------------------------------------------------------------------------
# 预设监控规则（开箱即用 / 一键开关）
# ---------------------------------------------------------------------------


@router.get("/presets", summary="预设监控规则列表")
async def monitor_presets_list(request: Request) -> Response:
    """列出全部预设规则与当前启停状态（供前端渲染开关控件）。"""
    container = _services(request)
    with Timer() as timer:
        statuses = container.monitor_engine.list_preset_statuses()
    return respond(statuses, "CN", elapsed_ms=timer.elapsed_ms)


@router.post("/presets/{preset_key}", summary="应用预设规则")
async def monitor_preset_apply(request: Request, preset_key: str) -> Response:
    """将某预设注入为真实规则（默认开启；已存在则幂等返回）。"""
    container = _services(request)
    with Timer() as timer:
        try:
            rule = container.monitor_engine.apply_preset(preset_key)
        except KeyError:
            raise StarletteHTTPException(status_code=404, detail=f"未知预设规则: {preset_key}")
    return respond(rule.to_dict(), rule.market, elapsed_ms=timer.elapsed_ms)


@router.post("/presets/{preset_key}/toggle", summary="一键开关预设规则")
async def monitor_preset_toggle(request: Request, preset_key: str) -> Response:
    """一键开关：已应用则切换 enabled；未应用则注入并开启。"""
    container = _services(request)
    with Timer() as timer:
        try:
            rule = container.monitor_engine.toggle_preset(preset_key)
        except KeyError:
            raise StarletteHTTPException(status_code=404, detail=f"未知预设规则: {preset_key}")
    return respond(rule.to_dict(), rule.market, elapsed_ms=timer.elapsed_ms)


# ---------------------------------------------------------------------------
# M12-M14 持仓
# ---------------------------------------------------------------------------


@router.get("/positions", summary="持仓盈亏（M12）")
async def monitor_positions(
    request: Request,
    market: str = "CN",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Response:
    """分页返回持仓盈亏视图（P0 无实时报价缓存，last 以成本价占位）。
    P1-2：LIMIT/OFFSET 下推到 SQLite，flat_position_view 仅对一页条目展开。
    """
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        limit, offset = page_limits(page, page_size)
        total = container.monitor_tracker.count_positions(code)
        records = container.monitor_tracker.list_positions(code, limit=limit, offset=offset)
        items = [flat_position_view(record) for record in records]
        payload = db_page(items, total, page, page_size)
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.post("/positions", summary="登记持仓（M13）")
async def monitor_positions_add(request: Request, body: PositionInput) -> Response:
    """登记一笔持仓并返回 PositionView（暂无实时报价，last 以成本价占位）。"""
    container = _services(request)
    code = resolve_market(container.config, body.market)
    if not str(body.code).strip():
        raise MissingKeyError("[fail-loud/NF-26] 持仓代码不能为空")
    position = Position(
        code=str(body.code).strip(),
        market=code,
        shares=float(body.shares),
        cost_price=float(body.cost_price),
        opened_at=body.opened_at if body.opened_at is not None else dt.date.today(),
    )
    with Timer() as timer:
        container.monitor_tracker.add_position(position, name=str(body.name or ""))
        record = container.monitor_tracker.get_record(position.code)
        payload = flat_position_view(record)
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.delete("/positions/{code}", summary="删除持仓（M14）")
async def monitor_positions_remove(
    request: Request, code: str, market: str = "CN"
) -> Response:
    """删除持仓；不存在 → 404。"""
    container = _services(request)
    market_code = resolve_market(container.config, market)
    with Timer() as timer:
        removed = container.monitor_tracker.remove_position(code)
    if not removed:
        raise StarletteHTTPException(
            status_code=404, detail=f"持仓不存在: {code}"
        )
    return respond({"removed": code}, market_code, elapsed_ms=timer.elapsed_ms)


# ---------------------------------------------------------------------------
# M15 告警历史
# ---------------------------------------------------------------------------


@router.get("/alerts", summary="告警历史（M15）")
async def monitor_alerts(
    request: Request,
    market: str = "CN",
    level: Literal["info", "warning", "critical"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Response:
    """分页返回告警历史（按时间倒序，可 level 过滤）。
    P1-2：LIMIT/OFFSET 下推到 SQLite（原实现一次取 10k 再内存切片）。
    """
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        limit, offset = page_limits(page, page_size)
        total = container.monitor_store.count_alerts(market=code, level=level)
        fetched = container.monitor_store.list_alerts(
            market=code, level=level, limit=limit, offset=offset
        )
        items = [alert.to_dict() for alert in fetched]
        payload = db_page(items, total, page, page_size)
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


# ---------------------------------------------------------------------------
# M16 通道
# ---------------------------------------------------------------------------


@router.get("/channels", summary="推送通道状态（M16）")
async def monitor_channels(request: Request) -> Response:
    """列出推送通道（desktop/webhook，其余 P1 置灰）。"""
    container = _services(request)
    with Timer() as timer:
        payload = {"items": container.monitor_notifier.channels_info()}
    return respond(payload, "CN", elapsed_ms=timer.elapsed_ms)


# ---------------------------------------------------------------------------
# M17 WebSocket
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


async def _ws_send(websocket: WebSocket, market: str, data: dict[str, Any]) -> None:
    """发送一帧合法 NF-9 信封（data 含 ``type`` 字段）。"""
    envelope = Envelope.ok(data, market=market, version=__version__)
    await websocket.send_text(envelope.to_json())


def _frame_type(payload: Any) -> str | None:
    """从客户端帧里解析 ``data.type``（兼容裸 ``{"type": ...}`` 与信封两种形态）。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("type"), str):
        return data["type"]
    if isinstance(payload.get("type"), str):
        return payload["type"]
    return None


#: M17 客户端帧接收轮询窗口（秒）：越小对心跳/超时响应越及时
_WS_POLL_TIMEOUT = 1.0


async def _handle_client_frame(
    websocket: WebSocket, market: str, text: str
) -> None:
    """处理客户端帧：ping → pong；其余仅刷新活动时间。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("WS 收到非法 JSON 帧（不静默）: %s", exc)
        return
    frame_type = _frame_type(payload)
    if frame_type == "ping":
        await _ws_send(websocket, market, {"type": "pong", "server_ts": _now_iso()})


async def _drain_alerts(
    websocket: WebSocket, market: str, queue: asyncio.Queue
) -> None:
    """把队列里积压的告警帧全部转发（非阻塞 get_nowait，不产生可取消任务）。"""
    while True:
        try:
            event = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        await _ws_send(websocket, market, event)


async def _ws_loop(websocket: WebSocket, market: str, queue: asyncio.Queue) -> None:
    """WS 主循环：转发告警 + 响应 ping + 30s 心跳 + 60s 超时关闭。

    队列用 ``get_nowait`` 非阻塞排空（**不创建可取消的 ``queue.get()`` 任务**，
    避免告警恰好落在取消窗口而丢失导致客户端挂起）；客户端接收用短超时
    ``wait_for`` 轮询，心跳/超时检查每轮执行。
    """
    last_activity = time.monotonic()
    last_ping = time.monotonic()
    while True:
        await _drain_alerts(websocket, market, queue)

        now = time.monotonic()
        if now - last_activity > CLIENT_TIMEOUT_SECONDS:
            logger.warning(
                "WS 客户端 %s 超时 %ss 无响应，关闭", market, CLIENT_TIMEOUT_SECONDS
            )
            return
        if now - last_ping >= PING_INTERVAL_SECONDS:
            await _ws_send(websocket, market, {"type": "ping"})
            last_ping = now

        try:
            text = await asyncio.wait_for(
                websocket.receive_text(), timeout=_WS_POLL_TIMEOUT
            )
        except asyncio.TimeoutError:
            continue
        except WebSocketDisconnect:
            return
        except RuntimeError:
            # 客户端已断开后再次 receive 会抛 RuntimeError（1006 根因之一），
            # 与 WebSocketDisconnect 同等对待为正常退出。
            logger.info("WS 客户端连接已关闭（receive RuntimeError）market=%s", market)
            return
        last_activity = time.monotonic()
        await _handle_client_frame(websocket, market, text)


@router.websocket("/ws")
async def monitor_ws(websocket: WebSocket, market: str = "CN") -> None:
    """M17 WebSocket：hello → snapshot → 实时 alert → ping/pong → bye。

    每客户端独立订阅 ``TOPIC_ALERT`` 并在关闭时退订（不跨客户端串流）。
    帧协议见契约 §2.4.1。

    **1006 防护**：``accept`` 之后的一切 handler 异常都走 fail-loud —— 记录
    日志并发送 bye 帧 + 显式关闭码（1011 服务端错误 / 1008 市场未启用 /
    1000 正常退出），**绝不静默异常关闭（1006）**。
    """
    try:
        await websocket.accept()
    except Exception as exc:  # noqa: BLE001 - accept 本身失败（客户端已断开）
        logger.warning("WS accept 失败: %s", exc)
        return
    try:
        container = get_services_from_app(websocket.app)
    except Exception as exc:  # noqa: BLE001 - 组合根装配失败 → 优雅 1011 而非 1006
        logger.error("WS 组合根装配失败（优雅关闭 1011）: %s", exc)
        try:
            await _ws_send(
                websocket, "CN",
                {"type": "bye", "reason": f"server container failed: {type(exc).__name__}: {exc}"},
            )
            await websocket.close(code=1011)
        except (WebSocketDisconnect, RuntimeError) as close_exc:
            logger.info("WS 关闭跳过（连接已断开）: %s", close_exc)
        return
    try:
        code = resolve_market(container.config, market)
    except Exception as exc:  # noqa: BLE001 - 市场未启用/非法 → bye 帧后关闭
        await _ws_send(websocket, "CN", {"type": "bye", "reason": str(exc)})
        await websocket.close(code=1008)
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()

    def _enqueue_alert(topic: str, event: Any) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError as exc:
            logger.warning("WS 告警转发失败（事件循环已关闭）: %s", exc)

    # 时序（QA 收口轮）：**先完成 EVENT_BUS 订阅（每客户端独立 Queue 桥接注册
    # 成功），再发 hello 帧** —— 保证 hello 之后收到的 alert 一定进队列不丢；
    # 订阅失败显式 fail-loud（bye 帧 + 关闭，不留半开连接）。
    try:
        unsubscribe = EVENT_BUS.subscribe(TOPIC_ALERT, _enqueue_alert)
    except Exception as exc:  # noqa: BLE001 - 订阅失败（如主题未声明）显式报错
        logger.error("WS 订阅 TOPIC_ALERT 失败: %s", exc)
        await _ws_send(websocket, code, {"type": "bye", "reason": f"subscribe failed: {exc}"})
        await websocket.close(code=1011)
        return
    try:
        await _ws_send(
            websocket,
            code,
            {"type": "hello", "market": code, "subscribed": ["alert"], "server_ts": _now_iso()},
        )
        history = container.monitor_store.list_alerts(market=code, limit=50)
        await _ws_send(
            websocket,
            code,
            {"type": "snapshot", "alerts": [alert.to_dict() for alert in history]},
        )
        await _ws_loop(websocket, code, queue)
    except WebSocketDisconnect:
        logger.info("WS 客户端断开 market=%s", code)
    except Exception as exc:  # noqa: BLE001 - handler 未预期异常 → 优雅 1011 而非 1006
        logger.error("WS 处理异常（优雅关闭 1011）market=%s: %s", code, exc)
        if websocket.client_state.name != "disconnected":
            try:
                await _ws_send(
                    websocket, code,
                    {"type": "bye", "reason": f"server error: {type(exc).__name__}: {exc}"},
                )
                await websocket.close(code=1011)
            except (WebSocketDisconnect, RuntimeError) as close_exc:
                logger.info("WS 关闭跳过（连接已断开）: %s", close_exc)
    finally:
        unsubscribe()
        if websocket.client_state.name != "disconnected":
            try:
                await _ws_send(websocket, code, {"type": "bye", "reason": "closing"})
            except (WebSocketDisconnect, RuntimeError) as exc:
                logger.info("WS bye 发送跳过（连接已关闭）: %s", exc)
            try:
                await websocket.close()
            except RuntimeError as exc:
                logger.info("WS close 幂等完成: %s", exc)
