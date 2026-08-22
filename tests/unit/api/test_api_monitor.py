"""monitor 路由单测（M1-M17）：生命周期 / 自选 / 规则 / 持仓 / 告警 / 通道 / WS。

全部用真监控组件（假 feed 避免网络）+ 真 MonitorStore（tmp），
M17 用 TestClient.websocket_connect 真握手验证 hello→snapshot→alert→ping/pong。
"""
from __future__ import annotations

import datetime as dt

import pytest
from envelope_validator import assert_envelope

from Kuantix.core.contracts import Alert, AlertLevel
from Kuantix.core.eventbus import EVENT_BUS, TOPIC_ALERT


def _add_alert(store, code: str = "600519", level: str = "warning") -> Alert:
    alert = Alert(
        id=f"al_{code}_{level}",
        code=code,
        market="CN",
        rule="止损-成本-8%",
        level=AlertLevel(level),
        message=f"{code} 跌破止损线",
        ts=dt.datetime(2026, 8, 1, 14, 52, 11, tzinfo=dt.timezone.utc),
        payload={"last": 1545.6, "cost": 1680.0},
    )
    store.add_alert(alert)
    return alert


# ---------------------------------------------------------------------------
# M1-M3 生命周期
# ---------------------------------------------------------------------------


def test_m3_status_shape(monitor_client) -> None:
    payload = monitor_client.get("/api/v1/monitor/status").json()
    assert_envelope(payload)
    data = payload["data"]
    assert data["running"] is False
    for key in (
        "started_at", "poll_interval_seconds", "trading_hours_only",
        "in_trading_session", "last_poll_at", "last_poll_ok",
        "consecutive_errors", "watchlist_count", "rules_enabled_count", "channels",
    ):
        assert key in data
    assert payload["meta"]["market"] == "CN"


def test_m1_start_without_watchlist_422(monitor_client) -> None:
    response = monitor_client.post("/api/v1/monitor/start")
    assert response.status_code == 422
    assert response.json()["code"] == 422


def test_m1_start_stop_lifecycle(monitor_client) -> None:
    monitor_client.post(
        "/api/v1/monitor/watchlist",
        json={"market": "CN", "codes": ["600519", "600036"], "source": "manual"},
    )
    start = monitor_client.post("/api/v1/monitor/start").json()
    assert start["data"]["running"] is True
    assert start["data"]["watchlist_count"] == 2
    stop = monitor_client.post("/api/v1/monitor/stop").json()
    assert stop["data"]["running"] is False
    status = monitor_client.get("/api/v1/monitor/status").json()
    assert status["data"]["running"] is False


def test_m1_start_hk_501(monitor_client) -> None:
    response = monitor_client.post("/api/v1/monitor/start?market=HK")
    assert response.status_code == 501
    assert response.json()["code"] == 501


# ---------------------------------------------------------------------------
# M4-M6 自选
# ---------------------------------------------------------------------------


def test_m4_watchlist_paginated(monitor_client) -> None:
    monitor_client.post(
        "/api/v1/monitor/watchlist", json={"codes": ["600519", "600036"]}
    )
    payload = monitor_client.get("/api/v1/monitor/watchlist?page=1&page_size=1").json()
    data = payload["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 1
    item = data["items"][0]
    for key in ("code", "name", "market", "added_at", "source"):
        assert key in item
    assert item["source"] == "manual"


def test_m5_watchlist_add_skipped(monitor_client, monitor_services) -> None:
    payload = monitor_client.post(
        "/api/v1/monitor/watchlist",
        json={"market": "CN", "codes": ["600519", "600036"], "source": "screen"},
    ).json()
    assert payload["data"]["added"] == ["600519", "600036"]
    assert payload["data"]["skipped"] == []
    codes = monitor_services.monitor.watchlist_codes("CN")
    assert codes == sorted(["600519", "600036"])  # 存储层按代码升序


def test_m5_watchlist_add_invalid_code_400(monitor_client) -> None:
    response = monitor_client.post(
        "/api/v1/monitor/watchlist", json={"codes": ["not-a-code"]}
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_m5_watchlist_add_over_limit_422(monitor_client) -> None:
    codes = [f"{i:06d}" for i in range(1, 102)]
    response = monitor_client.post(
        "/api/v1/monitor/watchlist", json={"codes": codes}
    )
    assert response.status_code == 422
    assert response.json()["code"] == 422


def test_m6_watchlist_remove(monitor_client) -> None:
    monitor_client.post("/api/v1/monitor/watchlist", json={"codes": ["600519"]})
    payload = monitor_client.delete("/api/v1/monitor/watchlist/600519").json()
    assert payload["data"] == {"removed": "600519"}
    # 再删 → 404
    response = monitor_client.delete("/api/v1/monitor/watchlist/600519")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# M7-M11 规则
# ---------------------------------------------------------------------------


def test_m7_criteria(monitor_client) -> None:
    payload = monitor_client.get("/api/v1/monitor/criteria").json()
    assert_envelope(payload)
    items = payload["data"]["items"]
    types = {item["type"] for item in items}
    # price / indicator / stop_loss 为基础判据；change_pct / volume 为预设规则
    # 依赖的扩展判据（插件式，自动出现在 criteria 列表，便于手动创建同类规则）
    assert types == {"price", "indicator", "stop_loss", "change_pct", "volume"}
    for item in items:
        assert "params_schema" in item


def test_m8_rules_list(monitor_client) -> None:
    monitor_client.post(
        "/api/v1/monitor/rules",
        json={
            "name": "止损-成本-8%",
            "market": "CN",
            "codes": ["600519"],
            "criterion_type": "stop_loss",
            "params": {"base": "cost", "pct": 0.08},
            "level": "critical",
            "cooldown_seconds": 300.0,
            "enabled": True,
        },
    )
    payload = monitor_client.get("/api/v1/monitor/rules").json()
    data = payload["data"]
    # 启动已注入 5 个预设规则（默认开启）+ 本测试自建 1 条 = 6
    assert data["total"] == 6
    # 自建规则仍存在
    manual = [r for r in data["items"] if r["preset_key"] is None]
    assert len(manual) == 1
    rule = manual[0]
    assert rule["name"] == "止损-成本-8%"
    assert rule["scope"] == {"market": "CN", "codes": ["600519"]}
    assert rule["criterion_type"] == "stop_loss"
    assert rule["level"] == "critical"
    assert rule["id"].startswith("rule_")
    # 预设规则已注入且默认开启、来源为 preset
    presets = [r for r in data["items"] if r["source"] == "preset"]
    assert len(presets) == 5
    assert all(r["enabled"] for r in presets)
    assert {r["preset_key"] for r in presets} == {
        "limit_up", "limit_down", "change_anomaly", "volume_anomaly", "key_level_break"
    }


def test_m9_add_rule_invalid_criterion_400(monitor_client) -> None:
    response = monitor_client.post(
        "/api/v1/monitor/rules",
        json={
            "name": "bad",
            "codes": ["600519"],
            "criterion_type": "bogus",
            "params": {},
            "level": "info",
        },
    )
    assert response.status_code == 400


def test_m9_add_rule_invalid_level_400(monitor_client) -> None:
    response = monitor_client.post(
        "/api/v1/monitor/rules",
        json={
            "name": "bad",
            "codes": ["600519"],
            "criterion_type": "price",
            "params": {"op": "above", "threshold": 100.0},
            "level": "extreme",
        },
    )
    assert response.status_code == 400


def test_m9_add_rule_empty_codes_400(monitor_client) -> None:
    response = monitor_client.post(
        "/api/v1/monitor/rules",
        json={
            "name": "bad",
            "codes": [],
            "criterion_type": "price",
            "params": {"op": "above", "threshold": 100.0},
            "level": "info",
        },
    )
    assert response.status_code == 400


def test_m10_update_rule(monitor_client) -> None:
    created = monitor_client.post(
        "/api/v1/monitor/rules",
        json={
            "name": "r1",
            "codes": ["600519"],
            "criterion_type": "price",
            "params": {"op": "above", "threshold": 100.0},
            "level": "info",
        },
    ).json()["data"]
    rule_id = created["id"]
    updated = monitor_client.put(
        f"/api/v1/monitor/rules/{rule_id}",
        json={"name": "r1-new", "enabled": False, "level": "warning"},
    ).json()["data"]
    assert updated["name"] == "r1-new"
    assert updated["enabled"] is False
    assert updated["level"] == "warning"
    assert updated["params"]["threshold"] == 100.0  # 未提供的字段保持原值


def test_m10_update_rule_not_found_404(monitor_client) -> None:
    response = monitor_client.put(
        "/api/v1/monitor/rules/rule_nope", json={"name": "x"}
    )
    assert response.status_code == 404


def test_m11_delete_rule(monitor_client) -> None:
    created = monitor_client.post(
        "/api/v1/monitor/rules",
        json={
            "name": "r1",
            "codes": ["600519"],
            "criterion_type": "price",
            "params": {"op": "above", "threshold": 100.0},
            "level": "info",
        },
    ).json()["data"]
    payload = monitor_client.delete(f"/api/v1/monitor/rules/{created['id']}").json()
    assert payload["data"] == {"removed": created["id"]}
    response = monitor_client.delete(f"/api/v1/monitor/rules/{created['id']}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# M12-M14 持仓
# ---------------------------------------------------------------------------


def test_m13_add_position_view_shape(monitor_client) -> None:
    payload = monitor_client.post(
        "/api/v1/monitor/positions",
        json={
            "code": "600519",
            "market": "CN",
            "shares": 100.0,
            "cost_price": 1680.0,
            "opened_at": "2026-01-05",
            "name": "贵州茅台",
        },
    ).json()
    assert_envelope(payload)
    view = payload["data"]
    assert view["code"] == "600519"
    assert view["shares"] == 100.0
    assert view["cost_price"] == 1680.0
    # P0 无实时报价：last 以成本价占位，change_pct/pnl 为 0（显式初始态）
    assert view["last"] == 1680.0
    assert view["change_pct"] == 0.0
    assert view["pnl"] == 0.0
    assert view["market_value"] == 168000.0
    assert view["as_of"]  # YYYY-MM-DD


def test_m12_positions_list_paginated(monitor_client) -> None:
    monitor_client.post(
        "/api/v1/monitor/positions",
        json={"code": "600519", "shares": 100.0, "cost_price": 1680.0},
    )
    monitor_client.post(
        "/api/v1/monitor/positions",
        json={"code": "600036", "shares": 200.0, "cost_price": 10.0},
    )
    payload = monitor_client.get("/api/v1/monitor/positions?page=1&page_size=1").json()
    data = payload["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 1


def test_m14_delete_position(monitor_client) -> None:
    monitor_client.post(
        "/api/v1/monitor/positions",
        json={"code": "600519", "shares": 100.0, "cost_price": 1680.0},
    )
    payload = monitor_client.delete("/api/v1/monitor/positions/600519").json()
    assert payload["data"] == {"removed": "600519"}
    response = monitor_client.delete("/api/v1/monitor/positions/600519")
    assert response.status_code == 404


def test_m13_add_position_invalid_shares_400(monitor_client) -> None:
    response = monitor_client.post(
        "/api/v1/monitor/positions",
        json={"code": "600519", "shares": -5.0, "cost_price": 1680.0},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# M15-M16 告警 / 通道
# ---------------------------------------------------------------------------


def test_m15_alerts_paginated_and_level_filter(monitor_client, monitor_services) -> None:
    store = monitor_services.monitor_store
    _add_alert(store, "600519", "critical")
    _add_alert(store, "600036", "info")
    payload = monitor_client.get("/api/v1/monitor/alerts?level=critical").json()
    assert_envelope(payload)
    data = payload["data"]
    assert data["total"] == 1
    assert data["items"][0]["level"] == "critical"
    assert data["items"][0]["rule"] == "止损-成本-8%"
    assert data["items"][0]["payload"]["cost"] == 1680.0
    all_payload = monitor_client.get("/api/v1/monitor/alerts").json()
    assert all_payload["data"]["total"] == 2


def test_m15_alerts_invalid_level_400(monitor_client) -> None:
    response = monitor_client.get("/api/v1/monitor/alerts?level=bogus")
    assert response.status_code == 400


def test_m16_channels(monitor_client) -> None:
    payload = monitor_client.get("/api/v1/monitor/channels").json()
    assert payload["data"]["items"] == []


# ---------------------------------------------------------------------------
# webhook 配置键（P0：config.toml [monitor].webhook_url 控制通道装配）
# ---------------------------------------------------------------------------


def _webhook_config(tmp_config):
    from dataclasses import replace

    return replace(
        tmp_config,
        monitor=replace(tmp_config.monitor, webhook_url="https://example.com/hook"),
    )


def test_build_monitor_channels_webhook_enabled(tmp_config) -> None:
    """配置 webhook_url 非空 → channels 含 desktop + webhook。"""
    from Kuantix.api.server import build_monitor

    loop, engine, tracker, store, notifier = build_monitor(_webhook_config(tmp_config))
    names = {ch.name for ch in notifier.channels}
    assert names == {"desktop", "webhook"}


def test_build_monitor_channels_webhook_disabled(tmp_config) -> None:
    """webhook_url 为空 → channels 只含 desktop（显式未启用，不伪造 URL）。"""
    from Kuantix.api.server import build_monitor

    loop, engine, tracker, store, notifier = build_monitor(tmp_config)
    names = {ch.name for ch in notifier.channels}
    assert names == {"desktop"}


def test_m16_channels_include_webhook_when_configured(tmp_config, jobs) -> None:
    """M16 channels 内容由配置决定：webhook_url 非空时列出 webhook。"""
    from conftest import FakeFactorService, FakeLake, FakeScreenService
    from fastapi.testclient import TestClient

    from Kuantix.api.deps import ServiceContainer
    from Kuantix.api.server import build_monitor, create_app

    cfg = _webhook_config(tmp_config)
    loop, engine, tracker, store, notifier = build_monitor(cfg)
    services = ServiceContainer(
        config=cfg,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        monitor=loop,
        monitor_engine=engine,
        monitor_tracker=tracker,
        monitor_store=store,
        monitor_notifier=notifier,
    )
    with TestClient(create_app(config=cfg, services=services)) as client:
        payload = client.get("/api/v1/monitor/channels").json()
        names = {item["name"] for item in payload["data"]["items"]}
        assert names == {"desktop", "webhook"}


def test_config_env_override_webhook_url(tmp_path, monkeypatch) -> None:
    """Kuantix__MONITOR__WEBHOOK_URL 环境变量可覆盖（对齐现有配置模式）。"""
    from conftest import make_config

    from Kuantix.config import load_config

    make_config(tmp_path)
    monkeypatch.setenv("Kuantix__MONITOR__WEBHOOK_URL", "https://hook.example.com/x")
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.monitor.webhook_url == "https://hook.example.com/x"


# ---------------------------------------------------------------------------
# M17 WebSocket
# ---------------------------------------------------------------------------


def test_m17_ws_hello_snapshot_alert_ping(
    monitor_client, monitor_services, clear_event_bus
) -> None:
    store = monitor_services.monitor_store
    _add_alert(store, "600519", "warning")
    with monitor_client.websocket_connect("/api/v1/monitor/ws?market=CN") as ws:
        hello = ws.receive_json()
        assert hello["code"] == 0
        assert hello["data"]["type"] == "hello"
        assert hello["data"]["market"] == "CN"
        assert hello["data"]["subscribed"] == ["alert"]
        assert "server_ts" in hello["data"]

        snapshot = ws.receive_json()
        assert snapshot["data"]["type"] == "snapshot"
        assert len(snapshot["data"]["alerts"]) == 1
        assert snapshot["data"]["alerts"][0]["code"] == "600519"

        # 注入实时告警（MonitorLoop._alert_frame 形状）
        EVENT_BUS.publish(
            TOPIC_ALERT,
            {"type": "alert", "alert": {"id": "al_live", "code": "600036", "market": "CN"}},
        )
        alert_frame = ws.receive_json()
        assert alert_frame["data"]["type"] == "alert"
        assert alert_frame["data"]["alert"]["code"] == "600036"

        # 客户端 ping → 服务端 pong
        ws.send_json({"type": "ping"})
        pong = ws.receive_json()
        assert pong["data"]["type"] == "pong"
        assert "server_ts" in pong["data"]


def test_m17_ws_envelope_frames(monitor_client, clear_event_bus) -> None:
    with monitor_client.websocket_connect("/api/v1/monitor/ws?market=CN") as ws:
        hello = ws.receive_json()
        assert_envelope(hello)
        snapshot = ws.receive_json()
        assert_envelope(snapshot)


def test_m17_ws_market_hk_bye_then_close(monitor_client, clear_event_bus) -> None:
    """市场未启用 → 服务端发 bye 帧后显式关闭（1008），而非异常 1006。

    回归验收（问题 5）：accept 后 ``resolve_market`` 失败时客户端收到带原因
    的 bye 帧，随后收到**明确关闭码 1008**（``WebSocketDisconnect.code``），
    绝不裸 1006 异常断开。
    """
    from starlette.websockets import WebSocketDisconnect

    with monitor_client.websocket_connect("/api/v1/monitor/ws?market=HK") as ws:
        bye = ws.receive_json()
        assert bye["data"]["type"] == "bye"
        assert "HK" in bye["data"]["reason"] or "未启用" in bye["data"]["reason"]
        # 服务端 close(code=1008) → 客户端 receive 抛 WebSocketDisconnect(code=1008)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 1008, "必须显式 1008 而非 1006"


def test_m17_ws_multi_client_no_crosstalk(monitor_client, clear_event_bus) -> None:
    """两个客户端各自订阅 TOPIC_ALERT，互不串流。"""
    with monitor_client.websocket_connect("/api/v1/monitor/ws?market=CN") as ws_a, \
         monitor_client.websocket_connect("/api/v1/monitor/ws?market=CN") as ws_b:
        _ = ws_a.receive_json()  # hello
        _ = ws_a.receive_json()  # snapshot
        _ = ws_b.receive_json()  # hello
        _ = ws_b.receive_json()  # snapshot
        EVENT_BUS.publish(TOPIC_ALERT, {"type": "alert", "alert": {"id": "al_1", "code": "600519"}})
        frame_a = ws_a.receive_json()
        frame_b = ws_b.receive_json()
        assert frame_a["data"]["type"] == "alert"
        assert frame_b["data"]["type"] == "alert"
        assert frame_a["data"]["alert"]["id"] == "al_1"
        assert frame_b["data"]["alert"]["id"] == "al_1"


def test_m17_ws_container_failure_graceful_1011(tmp_config, clear_event_bus) -> None:
    """WS 1006 防护：组合根装配失败 → bye 帧 + 显式关闭，而非异常 1006。

    回归验收（问题 5）：``accept`` 后 ``get_services_from_app`` 抛异常时，
    客户端收到带原因的 bye 帧（1011 语义），不会裸 1006 断开。
    """
    from fastapi.testclient import TestClient

    from Kuantix.api.server import create_app

    app = create_app(config=tmp_config)

    def boom_factory() -> None:
        raise RuntimeError("simulated container build failure")

    app.state.services_factory = boom_factory
    app.state.services = None

    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/monitor/ws?market=CN") as ws:
            bye = ws.receive_json()
            assert bye["code"] == 0
            assert bye["data"]["type"] == "bye"
            assert "server container failed" in bye["data"]["reason"]
            # 客户端随后收到显式关闭码 1011（不是 1006 异常断开）
            from starlette.websockets import WebSocketDisconnect

            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_json()
            assert excinfo.value.code == 1011, "必须显式 1011 而非 1006"


def test_m17_ws_handler_exception_graceful_1011(tmp_config, monitor_services, clear_event_bus) -> None:
    """WS 1006 防护：handler 内未预期异常 → bye 帧 + 显式关闭，而非 1006。"""
    import Kuantix.api.routers.monitor as monitor_router

    from fastapi.testclient import TestClient

    from Kuantix.api.server import create_app

    app = create_app(config=tmp_config, services=monitor_services)
    original = monitor_router._ws_send

    async def broken_send(websocket, market, data):
        if data.get("type") == "hello":
            raise RuntimeError("simulated handler failure")
        return await original(websocket, market, data)

    monitor_router._ws_send = broken_send
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/monitor/ws?market=CN") as ws:
                bye = ws.receive_json()
                assert bye["data"]["type"] == "bye"
                assert "server error" in bye["data"]["reason"]
                # 客户端随后收到显式关闭码 1011（不是 1006 异常断开）
                from starlette.websockets import WebSocketDisconnect

                with pytest.raises(WebSocketDisconnect) as excinfo:
                    ws.receive_json()
                assert excinfo.value.code == 1011, "必须显式 1011 而非 1006"
    finally:
        monitor_router._ws_send = original


# ---------------------------------------------------------------------------
# M11.1-M11.3 预设监控规则（一键开关）
# ---------------------------------------------------------------------------


def test_presets_listed_with_status(monitor_client) -> None:
    """预设列表返回全部 5 项，且启动已注入（applied=true, enabled=true）。"""
    payload = monitor_client.get("/api/v1/monitor/presets").json()
    assert_envelope(payload)
    items = payload["data"]
    assert len(items) == 5
    keys = {p["key"] for p in items}
    assert keys == {
        "limit_up", "limit_down", "change_anomaly", "volume_anomaly", "key_level_break"
    }
    # 启动已注入预设 → applied=true 且默认开启
    assert all(p["applied"] for p in items)
    assert all(p["enabled"] is True for p in items)
    # 展示字段齐全
    for p in items:
        assert p["name"] and p["description"]
        assert p["level"] in {"info", "warning", "critical"}
        assert p["rule_id"].startswith("rule_preset_")


def test_preset_toggle_off_then_on(monitor_client) -> None:
    """一键关闭 → enabled=false；再次切换 → enabled=true（持久化）。"""
    # 初始开启
    first = monitor_client.get("/api/v1/monitor/presets").json()["data"]
    assert next(p for p in first if p["key"] == "limit_up")["enabled"] is True

    # 关闭
    off = monitor_client.post("/api/v1/monitor/presets/limit_up/toggle").json()["data"]
    assert off["enabled"] is False
    assert off["preset_key"] == "limit_up"

    # 列表反映关闭态
    mid = monitor_client.get("/api/v1/monitor/presets").json()["data"]
    assert next(p for p in mid if p["key"] == "limit_up")["enabled"] is False

    # 再次切换 → 开启
    on = monitor_client.post("/api/v1/monitor/presets/limit_up/toggle").json()["data"]
    assert on["enabled"] is True


def test_preset_apply_unknown_key_404(monitor_client) -> None:
    """未知预设 key → 404。"""
    response = monitor_client.post("/api/v1/monitor/presets/does_not_exist")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_preset_toggle_persists_across_requests(monitor_client) -> None:
    """关闭后通过 /monitor/rules 列表也能看到该预设规则已禁用。"""
    monitor_client.post("/api/v1/monitor/presets/limit_down/toggle")
    rules = monitor_client.get("/api/v1/monitor/rules").json()["data"]["items"]
    ld = next(r for r in rules if r["preset_key"] == "limit_down")
    assert ld["enabled"] is False
