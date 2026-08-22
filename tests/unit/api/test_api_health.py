"""/health 与 /api/version 基础设施端点测试（契约 v1.1 R1.1-2）。

关键断言：``markets_enabled`` 必须是**对象**形状
``{"CN": true, "HK": false, "US": false}``（config 中启用为 true，
未启用为 false），而不是 T01 时期的列表。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from Kuantix.api.server import create_app
from Kuantix.config import Config


def _build_client(tmp_config: Config) -> TestClient:
    app = create_app(config=tmp_config)
    return TestClient(app)


def test_health_markets_enabled_is_object_shape(tmp_config: Config) -> None:
    client = _build_client(tmp_config)
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    data = payload["data"]
    assert data["status"] == "ok"
    assert isinstance(data["started_at"], str)
    assert isinstance(data["uptime_seconds"], (int, float))
    enabled = data["markets_enabled"]
    # v1.1 R1.1-2：对象形状，绝不允许是 list
    assert isinstance(enabled, dict)
    assert enabled == {"CN": True, "HK": False, "US": False}


def test_health_meta_market_default(tmp_config: Config) -> None:
    client = _build_client(tmp_config)
    payload = client.get("/health").json()
    assert payload["meta"]["market"] == "CN"
    assert payload["meta"]["version"] == "0.1.0"


def test_health_does_not_build_business_services(tmp_config: Config) -> None:
    """/health 零副作用：不触发组合根装配（不创建数据目录）。"""
    app = create_app(config=tmp_config)
    assert app.state.services is None
    assert app.state.services_factory is not None
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    # 仍保持惰性：未请求业务端点前 services 不装配
    assert app.state.services is None


def test_api_version_shape(tmp_config: Config) -> None:
    client = _build_client(tmp_config)
    payload = client.get("/api/version").json()
    assert payload["code"] == 0
    data = payload["data"]
    for key in ("name", "version", "upstream_easy_tdx", "config_source", "market_default"):
        assert key in data
    assert data["version"] == "0.1.0"
    assert data["market_default"] == "CN"


def test_unknown_route_returns_envelope_404(tmp_config: Config) -> None:
    client = _build_client(tmp_config)
    response = client.get("/api/v1/data/nope")
    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == 404
    assert set(payload) == {"code", "message", "data", "meta"}
