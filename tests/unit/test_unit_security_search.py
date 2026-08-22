"""SecuritySearchService 纯单测（不发网络，provider 注入假证券清单）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from Kuantix.core.contracts import Security
from Kuantix.core.fail_loud import DataIntegrityError, MissingKeyError
from Kuantix.data.security_search import SEARCH_LIMIT_MAX, SecuritySearchService
from tests.unit.api.conftest import make_config


def _securities() -> list[Security]:
    return [
        Security(code="600000", exchange="sh", market="CN", security_type="SH_A_STOCK", name="浦发银行"),
        Security(code="600036", exchange="sh", market="CN", security_type="SH_A_STOCK", name="招商银行"),
        Security(code="000001", exchange="sz", market="CN", security_type="SZ_A_STOCK", name="平安银行"),
        Security(code="000858", exchange="sz", market="CN", security_type="SZ_A_STOCK", name="五粮液"),
        Security(code="510300", exchange="sh", market="CN", security_type="SH_ETF", name="沪深300ETF"),
    ]


def _svc(tmp_path: Path, provider=None, cache_path: Path | None = None) -> SecuritySearchService:
    config = make_config(tmp_path)
    return SecuritySearchService(
        config,
        provider=provider or _securities,
        cache_path=cache_path or (tmp_path / "db" / "catalog.json"),
    )


def test_search_code_exact(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    hits = svc.search("600000")
    assert len(hits) == 1
    assert hits[0].code == "600000"
    assert hits[0].name == "浦发银行"
    assert hits[0].exchange == "sh"
    assert hits[0].market == "CN"
    assert hits[0].security_type == "SH_A_STOCK"


def test_search_code_prefix(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    hits = svc.search("6000")
    codes = [h.code for h in hits]
    assert codes == ["600000", "600036"]


def test_search_name(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    hits = svc.search("浦发")
    assert len(hits) == 1
    assert hits[0].code == "600000"


def test_search_name_fuzzy(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    hits = svc.search("银行")
    assert {h.code for h in hits} == {"600000", "600036", "000001"}


def test_search_name_whitespace_insensitive(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    hits = svc.search("  浦发  ")
    assert len(hits) == 1
    assert hits[0].code == "600000"


def test_search_no_match_empty(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.search("999999") == []


def test_search_limit(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    hits = svc.search("6000", limit=1)
    assert len(hits) == 1


def test_search_empty_q_raises(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    with pytest.raises(MissingKeyError):
        svc.search("   ")


def test_search_limit_zero_raises(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    with pytest.raises(MissingKeyError):
        svc.search("600000", limit=0)


def test_search_provider_failure_fail_loud(tmp_path: Path) -> None:
    """provider 抛异常且无缓存 → DataIntegrityError（不静默空返回）。"""

    def boom() -> list[Security]:
        raise ConnectionError("network down")

    svc = _svc(tmp_path, provider=boom)
    with pytest.raises(DataIntegrityError):
        svc.search("600000")


def test_catalog_cache_persisted(tmp_path: Path) -> None:
    """首次枚举落盘缓存，第二次直接读缓存（provider 不再调用）。"""
    calls = {"n": 0}

    def counting() -> list[Security]:
        calls["n"] += 1
        return _securities()

    cache = tmp_path / "db" / "catalog.json"
    svc1 = _svc(tmp_path, provider=counting, cache_path=cache)
    assert svc1.search("600000") != []
    assert calls["n"] == 1
    assert cache.is_file()

    svc2 = _svc(tmp_path, provider=counting, cache_path=cache)
    assert svc2.search("000858") != []
    assert calls["n"] == 1  # 缓存命中


def test_catalog_cache_corrupt_fail_loud(tmp_path: Path) -> None:
    """缓存文件损坏 → DataIntegrityError（fail-loud，不静默重建）。"""
    cache = tmp_path / "db" / "catalog.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("not-json{", encoding="utf-8")
    svc = _svc(tmp_path, cache_path=cache)
    with pytest.raises(DataIntegrityError):
        svc.catalog_size()


def test_search_limit_max_clamped(tmp_path: Path) -> None:
    """limit 超上限被收敛到 SEARCH_LIMIT_MAX，不抛错。"""
    svc = _svc(tmp_path)
    hits = svc.search("6", limit=999999)
    assert len(hits) <= SEARCH_LIMIT_MAX
