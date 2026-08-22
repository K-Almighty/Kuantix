"""D8 证券搜索端点单测（v1.2 增量）。

覆盖：
- 按代码精确 / 前缀 / 名称模糊搜索；
- 无匹配 → 显式空数组；
- q 为空 → 400；
- 清单源不可用（组合根缺 security_search）→ 501；
- 服务层缓存落盘（首次枚举后写 JSON，之后读缓存）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from envelope_validator import assert_envelope

from Kuantix.api.deps import ServiceContainer
from Kuantix.core.contracts import Security
from Kuantix.data.security_search import SecuritySearchService
from tests.unit.api.conftest import make_config, FakeLake, FakeFactorService, FakeScreenService


def _fake_securities() -> list[Security]:
    return [
        Security(code="600000", exchange="sh", market="CN", security_type="SH_A_STOCK", name="浦发银行"),
        Security(code="600036", exchange="sh", market="CN", security_type="SH_A_STOCK", name="招商银行"),
        Security(code="000001", exchange="sz", market="CN", security_type="SZ_A_STOCK", name="平安银行"),
        Security(code="000858", exchange="sz", market="CN", security_type="SZ_A_STOCK", name="五粮液"),
    ]


def _make_search_client(tmp_path: Path, services: ServiceContainer):
    from fastapi.testclient import TestClient
    from Kuantix.api.server import create_app

    app = create_app(config=None, services=services)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def search_client(tmp_path: Path, jobs):
    """带 security_search 的组合根（假 provider，不发网络）。"""
    config = make_config(tmp_path)
    search_service = SecuritySearchService(
        config,
        provider=_fake_securities,
        cache_path=tmp_path / "db" / "security_catalog.json",
    )
    container = ServiceContainer(
        config=config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        security_search=search_service,
    )
    app = None
    from fastapi.testclient import TestClient
    from Kuantix.api.server import create_app

    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        yield client


def test_d8_search_by_code_exact(search_client) -> None:
    payload = search_client.get("/api/v1/data/search", params={"q": "600000"}).json()
    assert_envelope(payload)
    data = payload["data"]
    assert data["count"] == 1
    hit = data["items"][0]
    assert hit["code"] == "600000"
    assert hit["name"] == "浦发银行"
    assert hit["exchange"] == "sh"
    assert hit["market"] == "CN"
    assert hit["security_type"] == "SH_A_STOCK"
    assert payload["meta"]["market"] == "CN"


def test_d8_search_by_code_prefix(search_client) -> None:
    payload = search_client.get("/api/v1/data/search", params={"q": "6000"}).json()
    assert_envelope(payload)
    codes = [h["code"] for h in payload["data"]["items"]]
    assert codes == ["600000", "600036"]


def test_d8_search_by_name(search_client) -> None:
    payload = search_client.get("/api/v1/data/search", params={"q": "浦发"}).json()
    assert_envelope(payload)
    assert payload["data"]["count"] == 1
    assert payload["data"]["items"][0]["code"] == "600000"


def test_d8_search_by_name_fuzzy(search_client) -> None:
    payload = search_client.get("/api/v1/data/search", params={"q": "银行"}).json()
    assert_envelope(payload)
    codes = {h["code"] for h in payload["data"]["items"]}
    assert "600000" in codes
    assert "600036" in codes
    assert "000001" in codes


def test_d8_search_no_match_returns_empty(search_client) -> None:
    payload = search_client.get("/api/v1/data/search", params={"q": "999999"}).json()
    assert_envelope(payload)
    assert payload["data"]["items"] == []
    assert payload["data"]["count"] == 0


def test_d8_search_empty_q_400(search_client) -> None:
    response = search_client.get("/api/v1/data/search", params={"q": "   "})
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_d8_search_limit_ge1(search_client) -> None:
    # limit=0 → FastAPI Query(ge=1) 校验失败 → 400
    response = search_client.get("/api/v1/data/search", params={"q": "6", "limit": 0})
    assert response.status_code == 400


def test_d8_search_missing_service_501(tmp_path: Path, jobs) -> None:
    """组合根缺 security_search → 显式 501（不静默空返回）。"""
    config = make_config(tmp_path)
    container = ServiceContainer(
        config=config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
    )
    from fastapi.testclient import TestClient
    from Kuantix.api.server import create_app

    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        response = client.get("/api/v1/data/search", params={"q": "600000"})
    assert response.status_code == 501
    assert response.json()["code"] == 501


def test_d8_search_cache_persisted(tmp_path: Path) -> None:
    """首次搜索触发枚举并落盘缓存；第二次从缓存读（provider 不再调用）。"""
    config = make_config(tmp_path)
    calls = {"n": 0}

    def counting_provider() -> list[Security]:
        calls["n"] += 1
        return _fake_securities()

    svc = SecuritySearchService(
        config,
        provider=counting_provider,
        cache_path=tmp_path / "db" / "security_catalog.json",
    )
    assert svc.search("600000") != []
    assert calls["n"] == 1
    assert (tmp_path / "db" / "security_catalog.json").is_file()

    svc2 = SecuritySearchService(
        config,
        provider=counting_provider,
        cache_path=tmp_path / "db" / "security_catalog.json",
    )
    assert svc2.search("000858") != []
    # 缓存命中：provider 未被再次调用
    assert calls["n"] == 1


def test_d8_search_market_hk_501(search_client) -> None:
    """P0 仅 CN：HK 未启用 → 501（resolve_market 门禁，接口先行）。"""
    response = search_client.get("/api/v1/data/search", params={"q": "600000", "market": "HK"})
    assert response.status_code == 501
    assert response.json()["code"] == 501
