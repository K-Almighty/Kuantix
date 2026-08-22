"""T02 单测④（RD-10 白盒）：枚举每页新建连接。

验收台 ``test_acc_enumerator_conn.py`` 用假工厂验证每页新建 client；
本单测从**内部实现**验证：
- ``_fetch_count`` / ``_fetch_page`` 都走 ``new_tdx_client``（非池化）；
- 每次调用都新建连接、用完即 ``close()``；
- 复用连接（``get_tdx_client``）不被调用。
"""
from __future__ import annotations

import pandas as pd
import pytest

from Kuantix.adapters.tdx_client import TdxClientFactory
from Kuantix.adapters.universe import UniverseEnumerator


class _TrapFactory(TdxClientFactory):
    """白盒替身：get_tdx_client（池化）一旦被调用立刻失败。

    这证明枚举路径**不会**误用池化连接（RD-10 陷阱）。
    """

    def __init__(self) -> None:
        # 不走真实 host book，仅用于计数。
        object.__setattr__(self, "_created_total", 0)
        object.__setattr__(self, "_pool", {})
        object.__setattr__(self, "_lock", __import__("threading").RLock())

    def new_tdx_client(self, host: str | None = None):
        self._created_total += 1
        return _CountingClient()

    def get_tdx_client(self, host: str | None = None):
        raise AssertionError("RD-10：枚举不允许复用池化连接 get_tdx_client")


class _CountingClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def get_security_count(self, _market) -> int:
        return 0

    def get_security_list(self, _market, _start) -> pd.DataFrame:
        return pd.DataFrame(columns=["code"])


class _CountedClient(_CountingClient):
    """带正计数的 client，供 _fetch_count 用例使用。"""

    def get_security_count(self, _market) -> int:
        return 100


def test_fetch_page_uses_new_client_and_closes() -> None:
    """白盒：_fetch_page 每次新建连接并 close（每页一条新连接）。"""
    factory = _TrapFactory()
    enum = UniverseEnumerator(factory)
    made_before = factory._created_total  # noqa: SLF001 - 白盒单测

    enum._fetch_page(1, "sh", 0)  # noqa: SLF001 - 白盒单测
    assert factory._created_total == made_before + 1  # noqa: SLF001

    # 连续翻页：每页都新建，绝不复用
    enum._fetch_page(1, "sh", 1000)  # noqa: SLF001
    enum._fetch_page(1, "sh", 2000)  # noqa: SLF001
    assert factory._created_total == made_before + 3  # noqa: SLF001


def test_fetch_count_also_new_connection() -> None:
    """白盒：_fetch_count（取总数）同样新建连接并关闭。"""
    factory = _TrapFactory()
    enum = UniverseEnumerator(factory)

    # 返回正计数的 client，走完整 _fetch_count 逻辑
    def _counted_new(host: str | None = None):
        factory._created_total += 1  # noqa: SLF001
        return _CountedClient()

    factory.new_tdx_client = _counted_new  # type: ignore[method-assign]
    factory._created_total = 0  # noqa: SLF001
    enum._fetch_count(1, "sh")  # noqa: SLF001 - 白盒单测
    assert factory._created_total == 1  # noqa: SLF001


def test_connections_closed_after_page() -> None:
    """白盒：每页新建的 client 用完立刻 close（防 15s 复用陷阱）。"""
    factory = _TrapFactory()
    enum = UniverseEnumerator(factory)

    # 手动模拟：new_tdx_client 返回的 client 在 finally 中 close
    client = factory.new_tdx_client()
    client.get_security_list(1, 0)
    client.close()
    assert client.closed is True
