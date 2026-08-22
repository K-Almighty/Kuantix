"""验收标准④（T02 / RD-10）：枚举每页新建连接，耗时 < 1s/页。

独立验证手段：实测数据——复用连接 15.36s/页 vs 每页新建 0.03s/页（512× 差距）。
这是本机最容易踩的「连接复用陷阱」。

**离线替代验证**：注入假工厂，让 ``new_tdx_client()`` 每次返回**全新**实例并计数，
断言 ``_fetch_page`` 每翻一页确实构造了新的 client（而不是复用同一个）——
``len(made) == len(set(made))`` 且 ``>= 1``，且用完即 ``close()``。

代码依据（2026-08-01 源码核验）：
- ``universe.py:_fetch_page``：``client = self._factory.new_tdx_client()`` → ``finally: client.close()``
- 注意是 ``new_tdx_client``（非池化、每页新建），不是 ``get_tdx_client``（池化、复用）。
"""
from __future__ import annotations

import pytest

from _acc_common import import_optional


class _CountingClient:
    """最小假客户端：返回空清单，记录 close() 调用。"""

    def __init__(self, tag: int) -> None:
        self.tag = tag
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def get_security_list(self, _market, _start):
        import pandas as pd

        return pd.DataFrame(columns=["code"])  # 空清单：离线不触发网络，但走完整调用链

    def get_security_count(self, _market):
        raise AssertionError("离线替代验证不应触发 get_security_count")


class _CountingFactory:
    """假工厂：每次 new_tdx_client 返回全新实例并记录。"""

    def __init__(self) -> None:
        self.made: list[_CountingClient] = []

    def new_tdx_client(self, host: str | None = None) -> _CountingClient:
        client = _CountingClient(len(self.made))
        self.made.append(client)
        return client


def test_enumerator_new_client_per_page_offline(tmp_path):
    UE = import_optional("Kuantix.adapters.universe")

    factory = _CountingFactory()
    enum = UE.UniverseEnumerator(factory, max_pages_per_market=1)
    made_before = len(factory.made)

    # 触发 1 页：应恰好新建 1 个客户端（RD-10：每页新连接）
    enum._fetch_page(0, "sh", 0)  # noqa: SLF001 - 单页内部路径，离线白盒验证
    made_after = len(factory.made)
    assert made_after == made_before + 1, (
        f"_fetch_page 每页应新建 1 个连接（RD-10），实际新增 {made_after - made_before}"
    )

    new_client = factory.made[-1]
    assert new_client.closed, "RD-10 每页连接用完必须立刻 close（否则被服务端认作复用，触发 15s 陷阱）"

    # 连续两页：两次实例必须不同（非复用）
    enum._fetch_page(0, "sh", 0)
    enum._fetch_page(0, "sh", 0)
    assert len(factory.made) == made_before + 3
    assert len(set(id(c) for c in factory.made)) == len(factory.made), (
        "检测到复用同一客户端实例，违反 RD-10（复用 15.36s/页 vs 新建 0.03s/页）"
    )
