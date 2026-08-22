"""SecuritySearchService 本地化单测（T04 问题 1：零请求路径网络）。

覆盖（设计文档 08 §2.4）：
- 无本地清单（SQLite 空 + JSON 缺失 + 无 provider）→ DataIntegrityError（422）；
- 有 SQLite 清单 → 精确 / 前缀 / 名称命中；
- catalog JSON 导入兼容（D9 读兼容一版）；
- 构造后任何路径不 import UniverseEnumerator / TdxClientFactory（网络零调用）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from Kuantix.core.contracts import Security
from Kuantix.core.fail_loud import DataIntegrityError
from Kuantix.data.market_store import MarketStore
from Kuantix.data.security_search import SecuritySearchService
from tests.unit.api.conftest import make_config


def _securities() -> list[Security]:
    return [
        Security(code="600000", exchange="sh", market="CN", security_type="SH_A_STOCK", name="浦发银行"),
        Security(code="600036", exchange="sh", market="CN", security_type="SH_A_STOCK", name="招商银行"),
        Security(code="000001", exchange="sz", market="CN", security_type="SZ_A_STOCK", name="平安银行"),
    ]


def test_no_local_list_422(tmp_path: Path) -> None:
    """SQLite 空 + JSON 缺失 + 无 provider → DataIntegrityError（→ 422）。"""
    config = make_config(tmp_path)
    store = MarketStore(tmp_path / "db" / "market.db")
    svc = SecuritySearchService(config, store=store)
    with pytest.raises(DataIntegrityError) as excinfo:
        svc.search("600000")
    assert "data sync" in str(excinfo.value) or "data migrate" in str(excinfo.value)


def test_sqlite_list_exact_prefix_name(tmp_path: Path) -> None:
    """有 SQLite 清单 → 精确 / 前缀 / 名称命中（零 provider，零网络）。"""
    config = make_config(tmp_path)
    store = MarketStore(tmp_path / "db" / "market.db")
    store.upsert_securities(_securities())
    svc = SecuritySearchService(config, store=store)
    assert [h.code for h in svc.search("600000")] == ["600000"]
    assert [h.code for h in svc.search("6000")] == ["600000", "600036"]
    # 名称子串命中（顺序不敏感，与既有测试约定一致）
    assert {h.code for h in svc.search("银行")} == {"600000", "600036", "000001"}
    assert svc.catalog_size() == 3


def test_sqlite_list_market_isolation(tmp_path: Path) -> None:
    """market=HK 清单隔离（无 HK 数据 → 空匹配，合法态）。"""
    config = make_config(tmp_path)
    store = MarketStore(tmp_path / "db" / "market.db")
    store.upsert_securities(_securities())
    svc = SecuritySearchService(config, store=store)
    assert svc.search("600000", market="HK") == []


def test_catalog_json_read_compat(tmp_path: Path) -> None:
    """D9：SQLite 空但旧 JSON 存在 → 读 JSON 兜底（兼容一版）。"""
    import json

    config = make_config(tmp_path)
    cache = tmp_path / "db" / "security_catalog.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps([s.to_dict() for s in _securities()], ensure_ascii=False),
        encoding="utf-8",
    )
    store = MarketStore(tmp_path / "db" / "market.db")
    svc = SecuritySearchService(config, store=store, cache_path=cache)
    assert [h.code for h in svc.search("6000")] == ["600000", "600036"]


def test_no_network_imports_in_module() -> None:
    """构造路径零网络：模块 import 不含 UniverseEnumerator / TdxClientFactory。"""
    import ast

    import Kuantix.data.security_search as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
    assert not any("universe" in name.lower() for name in imports)
    assert not any("tdx_client" in name.lower() for name in imports)


def test_provider_alias_still_works(tmp_path: Path) -> None:
    """provider 作为测试别名仍可用（向后兼容）。"""
    config = make_config(tmp_path)
    svc = SecuritySearchService(config, provider=_securities)
    assert svc.search("600000")[0].name == "浦发银行"
