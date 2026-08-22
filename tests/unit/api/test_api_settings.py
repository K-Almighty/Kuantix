"""settings 路由单测（E1–E2，P2 只读）：信封 / 字段完整性 / known_hosts 只读 /
连通性测试业务结果 / 400 校验。

NF-20 守卫：
- E1 读 ``~/.easy_tdx/config.json`` 前后 sha256 指纹不变（用 tmp 复制 config
  验证，绝不触碰真实上游文件）；
- E2 全链路注入假工厂（成功/失败/超时），断言**不触碰**上游文件与真实网络；
- 红线：R3（无写 ~/.easy_tdx）、R4（无 except:pass / 双参 .get）、
  R5（settings 路径无 order/trade/buy/sell 语义）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from envelope_validator import assert_envelope

from tests.unit.api.conftest import (
    FakeFactorService,
    FakeLake,
    FakeScreenService,
    make_config,
)
from Kuantix.adapters import known_hosts as kh
from Kuantix.api.deps import ServiceContainer
from Kuantix.api.jobs import JobManager, JobStore
from Kuantix.api.server import create_app

STATUS_URL = "/api/v1/settings/status"
TEST_URL = "/api/v1/settings/test-connection"


class FakeTdxFactory:
    """TdxClientFactory 替身：``probe_connection`` 不发网络，返回预设业务结果。

    记录调用参数（kind/host/port/timeout），供测试断言 E2 走显式 host/port
    且超时为短超时（2.0s），**绝无** from_best_host / save_best_*。
    """

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def probe_connection(
        self, kind: str, *, host: str, port: int, timeout: float
    ) -> dict[str, Any]:
        self.calls.append({"kind": kind, "host": host, "port": port, "timeout": timeout})
        if self._result is None:
            return {"ok": True, "latency_ms": 12, "error": None}
        return dict(self._result)


def _build_client(
    tmp_path: Path,
    tdx_factory: FakeTdxFactory | None,
    *,
    upstream: dict[str, Any] | None = None,
) -> tuple[TestClient, Path]:
    """构造 settings 测试客户端：假 lake/factor/screen + 假/缺省 tdx_factory。

    Args:
        tmp_path: pytest tmp 目录（config 与假上游 config.json 全落在 tmp）。
        tdx_factory: 注入的假工厂；``None`` 模拟「组合根未装配」。
        upstream: 假 ``~/.easy_tdx/config.json`` 内容；``None`` 表示文件不存在。

    Returns:
        ``(client, upstream_path)`` —— upstream_path 供测试 monkeypatch
        ``kh.EASY_TDX_CONFIG_PATH`` 并做指纹自证。
    """
    config = make_config(tmp_path)
    jobs = JobManager(JobStore(tmp_path / "db"))
    services = ServiceContainer(
        config=config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        tdx_factory=tdx_factory,
    )
    app = create_app(config=config, services=services)
    upstream_path = tmp_path / ".easy_tdx" / "config.json"
    if upstream is not None:
        upstream_path.parent.mkdir(parents=True, exist_ok=True)
        upstream_path.write_text(json.dumps(upstream, ensure_ascii=False), encoding="utf-8")
    return TestClient(app), upstream_path


# ---------------------------------------------------------------------------
# E1 只读状态
# ---------------------------------------------------------------------------


def test_e1_status_shape(tmp_path, monkeypatch) -> None:
    upstream = {
        "best_host": "180.153.18.170",
        "known_hosts": ["119.147.212.81", "180.153.18.171"],
        "best_mac_host": "123.60.47.136",
        "mac_hosts": ["121.36.248.138"],
        "mac_ex_hosts": ["116.205.135.205"],
    }
    client, upstream_path = _build_client(tmp_path, None, upstream=upstream)
    monkeypatch.setattr(kh, "EASY_TDX_CONFIG_PATH", upstream_path)

    response = client.get(STATUS_URL)
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    data = payload["data"]
    assert payload["code"] == 0

    # 整页只读声明（NF-20）
    assert data["read_only"] is True

    # config 摘要：路径 / 默认市场 / 端口（不泄漏敏感信息）
    cfg = data["config"]
    assert cfg["default_market"] == "CN"
    assert cfg["enabled_markets"] == ["CN"]
    for key in ("root", "vipdoc", "db", "logs", "reports", "exports", "factors"):
        assert key in cfg["paths"]
    tdx = cfg["tdx"]
    assert tdx["port"] == 7709
    assert tdx["ex_port"] == 7727
    assert tdx["timeout_seconds"] > 0
    assert len(tdx["std_hosts"]) >= 1
    assert len(tdx["mac_hosts"]) >= 1
    assert len(tdx["mac_ex_hosts"]) >= 1

    # known_hosts：只读列表，含 host/port/kind + read_only 标注
    known = data["known_hosts"]
    assert known["upstream_available"] is True
    assert known["known_hosts_merged"] is True
    assert known["upstream_config_untouched"] is True
    assert len(known["items"]) >= 3
    for row in known["items"]:
        assert row["host"]
        assert row["port"] > 0
        assert row["kind"] in ("std", "mac", "mac_ex")
        assert row["read_only"] is True

    # 数据湖摘要：coverage.securities / files / bars + latest_job
    lake = data["data"]
    assert lake["market"] == "CN"
    assert lake["coverage"]["securities"] == 2
    assert lake["coverage"]["files"] == 2
    assert lake["coverage"]["bars"] == 4800
    assert "latest_job" in lake
    assert lake["latest_job"] is None

    # 版本：Kuantix + 上游锁定版本
    versions = data["versions"]
    assert versions["Kuantix"] == "0.1.0"
    assert versions["upstream_easy_tdx"] == "1.20.3"


def test_e1_known_hosts_readonly_fingerprint(tmp_path, monkeypatch) -> None:
    """NF-20 自证：E1 读上游 config.json 前后指纹（sha256/size/mtime）不变。"""
    upstream = {
        "best_host": "180.153.18.170",
        "known_hosts": ["119.147.212.81"],
        "mac_hosts": ["123.60.47.136"],
        "mac_ex_hosts": ["116.205.135.205"],
    }
    client, upstream_path = _build_client(tmp_path, None, upstream=upstream)
    monkeypatch.setattr(kh, "EASY_TDX_CONFIG_PATH", upstream_path)

    before = kh.fingerprint(upstream_path)
    assert before.exists is True

    response = client.get(STATUS_URL)
    assert response.status_code == 200
    assert response.json()["code"] == 0

    after = kh.fingerprint(upstream_path)
    assert after.exists == before.exists
    assert after.size == before.size
    assert after.mtime_ns == before.mtime_ns
    assert after.sha256 == before.sha256
    kh.assert_untouched(before)  # 显式断言：读前后零写入


def test_e1_upstream_missing_falls_back_to_builtin(tmp_path, monkeypatch) -> None:
    """上游 config.json 不存在 → 回退项目内置兜底清单（自给自足部署，不再缺失）。"""
    client, upstream_path = _build_client(tmp_path, None, upstream=None)
    monkeypatch.setattr(kh, "EASY_TDX_CONFIG_PATH", upstream_path)

    response = client.get(STATUS_URL)
    assert response.status_code == 200
    data = response.json()["data"]
    known = data["known_hosts"]
    assert known["upstream_available"] is False
    # 缺失不再等于「合入失败」：自动回退到项目内置兜底清单
    assert known["upstream_source"] == "builtin"
    assert known["known_hosts_merged"] is True
    assert known["upstream_config_untouched"] is True
    # config.toml 显式节点 + 内置兜底节点都被展示
    assert len(known["items"]) >= 3


def test_e1_does_not_touch_real_upstream(tmp_path, monkeypatch) -> None:
    """守卫：E1 只读路径绝不触碰真实 ``~/.easy_tdx/config.json``。"""
    real_path = Path.home() / ".easy_tdx" / "config.json"
    real_before = kh.fingerprint(real_path)  # 真实文件基线（存在或不存在）

    client, upstream_path = _build_client(tmp_path, None, upstream=None)
    monkeypatch.setattr(kh, "EASY_TDX_CONFIG_PATH", upstream_path)
    response = client.get(STATUS_URL)
    assert response.status_code == 200

    real_after = kh.fingerprint(real_path)
    assert real_after.exists == real_before.exists
    assert real_after.sha256 == real_before.sha256


# ---------------------------------------------------------------------------
# E2 连通性测试（只测不写）
# ---------------------------------------------------------------------------


def test_e2_success(tmp_path, monkeypatch) -> None:
    fake = FakeTdxFactory({"ok": True, "latency_ms": 42, "error": None})
    client, upstream_path = _build_client(tmp_path, fake, upstream=None)
    monkeypatch.setattr(kh, "EASY_TDX_CONFIG_PATH", upstream_path)

    response = client.post(
        TEST_URL, json={"kind": "mac", "host": "123.60.47.136", "port": 7709}
    )
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    assert payload["code"] == 0
    data = payload["data"]
    assert data["ok"] is True
    assert data["host"] == "123.60.47.136"
    assert data["port"] == 7709
    assert data["kind"] == "mac"
    assert data["latency_ms"] == 42
    assert data["error"] is None

    # 断言工厂被以显式 host/port + 短超时（2s）调用
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["host"] == "123.60.47.136"
    assert call["port"] == 7709
    assert call["kind"] == "mac"
    assert call["timeout"] == 2.0


def test_e2_failure_is_business_result_not_http_error(tmp_path, monkeypatch) -> None:
    """连接失败 → **业务结果**（code=0 信封 + ok=false + error），非 HTTP 错误。"""
    fake = FakeTdxFactory(
        {"ok": False, "latency_ms": None, "error": "ConnectionRefusedError: connect refused"}
    )
    client, upstream_path = _build_client(tmp_path, fake, upstream=None)
    monkeypatch.setattr(kh, "EASY_TDX_CONFIG_PATH", upstream_path)

    response = client.post(
        TEST_URL, json={"kind": "std", "host": "203.0.113.9", "port": 7709}
    )
    assert response.status_code == 200  # 不是 HTTP 错误
    payload = response.json()
    assert payload["code"] == 0
    data = payload["data"]
    assert data["ok"] is False
    assert data["latency_ms"] is None
    assert data["error"] == "ConnectionRefusedError: connect refused"


def test_e2_timeout_is_business_result(tmp_path, monkeypatch) -> None:
    fake = FakeTdxFactory(
        {"ok": False, "latency_ms": None, "error": "socket.timeout: timed out"}
    )
    client, upstream_path = _build_client(tmp_path, fake, upstream=None)
    monkeypatch.setattr(kh, "EASY_TDX_CONFIG_PATH", upstream_path)

    response = client.post(
        TEST_URL, json={"kind": "mac_ex", "host": "116.205.135.205", "port": 7727}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ok"] is False
    assert "timeout" in data["error"].lower()


def test_e2_invalid_kind_400(tmp_path, monkeypatch) -> None:
    fake = FakeTdxFactory()
    client, upstream_path = _build_client(tmp_path, fake, upstream=None)
    monkeypatch.setattr(kh, "EASY_TDX_CONFIG_PATH", upstream_path)

    response = client.post(
        TEST_URL, json={"kind": "bogus", "host": "1.2.3.4", "port": 7709}
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == 400
    assert len(fake.calls) == 0  # 校验失败，未触达工厂


def test_e2_invalid_port_400(tmp_path, monkeypatch) -> None:
    fake = FakeTdxFactory()
    client, upstream_path = _build_client(tmp_path, fake, upstream=None)
    monkeypatch.setattr(kh, "EASY_TDX_CONFIG_PATH", upstream_path)

    response = client.post(
        TEST_URL, json={"kind": "mac", "host": "1.2.3.4", "port": 0}
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_e2_missing_factory_400(tmp_path, monkeypatch) -> None:
    """组合根缺 tdx_factory → 显式 400（fail-loud，不静默）。"""
    client, upstream_path = _build_client(tmp_path, None, upstream=None)
    monkeypatch.setattr(kh, "EASY_TDX_CONFIG_PATH", upstream_path)

    response = client.post(
        TEST_URL, json={"kind": "mac", "host": "1.2.3.4", "port": 7709}
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == 400
    assert "tdx_factory" in payload["message"]


def test_e2_does_not_write_upstream(tmp_path, monkeypatch) -> None:
    """R3 守卫：E2 调用前后上游 config.json 指纹不变（零写入）。"""
    upstream = {"best_host": "180.153.18.170", "mac_hosts": ["123.60.47.136"]}
    fake = FakeTdxFactory({"ok": True, "latency_ms": 5, "error": None})
    client, upstream_path = _build_client(tmp_path, fake, upstream=upstream)
    monkeypatch.setattr(kh, "EASY_TDX_CONFIG_PATH", upstream_path)

    before = kh.fingerprint(upstream_path)
    response = client.post(
        TEST_URL, json={"kind": "mac", "host": "123.60.47.136", "port": 7709}
    )
    assert response.status_code == 200
    after = kh.fingerprint(upstream_path)
    assert after.sha256 == before.sha256
    assert after.size == before.size
    kh.assert_untouched(before)


# ---------------------------------------------------------------------------
# R5：settings 路由无下单语义
# ---------------------------------------------------------------------------


def test_settings_routes_have_no_trading_semantics() -> None:
    """R5 守卫：settings 路径不含 order/trade/buy/sell 语义。"""
    from Kuantix.api.routers.settings import router

    for route in router.routes:
        path = getattr(route, "path", "")
        lowered = path.lower()
        for token in ("order", "trade", "buy", "sell"):
            assert token not in lowered, f"settings 路径含下单语义: {path}"
