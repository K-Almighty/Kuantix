"""StrategyStore 单测（CRUD + 持久化 + 404 语义 + fail-loud 校验）。

全部落在 tmp_path，不触碰 ~/.Kuantix（NF-15 位置由实现保证）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from Kuantix.backtest.strategy_store import StrategyStore
from Kuantix.core.fail_loud import MissingKeyError


@pytest.fixture()
def store(tmp_path: Path) -> StrategyStore:
    return StrategyStore(tmp_path / "db" / "strategies.db")


def _payload(**overrides):
    payload = {
        "name": "双均线-茅台",
        "kind": "single",
        "strategy": "ma_cross",
        "strategy_label": "双均线交叉",
        "params": {"fast": 5, "slow": 20},
        "context": {"symbol": "SH:600519"},
        "trade_config": {"cash": 1000000},
        "snapshot": {"total_return": 0.26},
        "tags": ["优选"],
        "notes": "测试",
    }
    payload.update(overrides)
    return payload


def test_create_returns_full_view(store: StrategyStore) -> None:
    view = store.create(_payload())
    assert view["id"].startswith("strat_")
    assert view["name"] == "双均线-茅台"
    assert view["kind"] == "single"
    assert view["params"] == {"fast": 5, "slow": 20}
    assert view["context"] == {"symbol": "SH:600519"}
    assert view["tags"] == ["优选"]
    assert view["created_at"]
    assert view["updated_at"]


def test_get_roundtrip(store: StrategyStore) -> None:
    view = store.create(_payload())
    loaded = store.get(view["id"])
    assert loaded is not None
    assert loaded["id"] == view["id"]
    assert loaded["snapshot"] == {"total_return": 0.26}


def test_get_unknown_returns_none(store: StrategyStore) -> None:
    assert store.get("strat_nope_123") is None


def test_delete(store: StrategyStore) -> None:
    view = store.create(_payload())
    assert store.delete(view["id"]) is True
    assert store.get(view["id"]) is None
    # 再删一次 → False（路由层映射 404）
    assert store.delete(view["id"]) is False


def test_delete_unknown_returns_false(store: StrategyStore) -> None:
    assert store.delete("strat_nope_123") is False


def test_persistence_across_reopen(tmp_path: Path) -> None:
    """重启可见（同库文件重新打开能看到之前写入的策略）。"""
    db_path = tmp_path / "db" / "strategies.db"
    store1 = StrategyStore(db_path)
    view = store1.create(_payload(name="持久化测试"))
    store1.close()

    store2 = StrategyStore(db_path)
    loaded = store2.get(view["id"])
    assert loaded is not None
    assert loaded["name"] == "持久化测试"
    store2.close()


def test_create_missing_name_fails(store: StrategyStore) -> None:
    with pytest.raises(MissingKeyError):
        store.create(_payload(name=""))


def test_create_invalid_kind_fails(store: StrategyStore) -> None:
    with pytest.raises(MissingKeyError):
        store.create(_payload(kind="bogus"))


def test_create_missing_strategy_fails(store: StrategyStore) -> None:
    payload = _payload()
    del payload["strategy"]
    with pytest.raises(MissingKeyError):
        store.create(payload)


def test_list_pagination_and_kind_filter(store: StrategyStore) -> None:
    for i in range(5):
        store.create(_payload(name=f"策略{i}", kind="single"))
    for i in range(3):
        store.create(_payload(name=f"组合{i}", kind="portfolio"))

    page1 = store.list(page=1, page_size=4)
    assert page1["total"] == 8
    assert len(page1["items"]) == 4
    assert page1["total_pages"] == 2

    page2 = store.list(page=2, page_size=4)
    assert len(page2["items"]) == 4

    only_portfolio = store.list(kind="portfolio", page=1, page_size=50)
    assert only_portfolio["total"] == 3
    assert all(item["kind"] == "portfolio" for item in only_portfolio["items"])


def test_list_invalid_kind_fails(store: StrategyStore) -> None:
    with pytest.raises(MissingKeyError):
        store.list(kind="bogus")


def test_update_fields(store: StrategyStore) -> None:
    view = store.create(_payload())
    updated = store.update(view["id"], {"name": "改名", "snapshot": {"total_return": 0.5}})
    assert updated is not None
    assert updated["name"] == "改名"
    assert updated["snapshot"] == {"total_return": 0.5}
    # 未提供的字段保持不变
    assert updated["strategy"] == "ma_cross"
    assert updated["context"] == {"symbol": "SH:600519"}


def test_update_unknown_returns_none(store: StrategyStore) -> None:
    assert store.update("strat_nope_123", {"name": "x"}) is None


def test_update_invalid_kind_fails(store: StrategyStore) -> None:
    view = store.create(_payload())
    with pytest.raises(MissingKeyError):
        store.update(view["id"], {"kind": "bogus"})
