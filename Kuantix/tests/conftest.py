"""测试夹具与离线替身（上游只读守卫 + RD-10 连接陷阱模拟器）。

设计原则
--------
1. **全部离线可复现**：不依赖外网 TDX 服务器。真实链路用例打
   ``@pytest.mark.network``，通过 ``Kuantix_TEST_NETWORK=1`` 显式开启。
2. **不碰上游**：``_upstream_readonly_guard`` 在整个 session 前后各做一次
   指纹快照，任何对 ``~/.easy_tdx/`` 或 easy-tdx 包目录的写入都会让测试失败。
3. **不碰真实数据根**：所有落盘一律写 ``tmp_path``，绝不写 ``~/.Kuantix/``。
4. **替身要能"复现故障"**：:class:`FakeTdxClient` 内建 RD-10 的连接复用陷阱
   —— 同一条连接第 2 次 ``get_security_list`` 会被罚以 2s 延迟。若
   :class:`~Kuantix.adapters.universe.UniverseEnumerator` 哪天被改成复用连接，
   验收④ 的 ``<1s/页`` 立刻失败。这是"测试能抓到回归"而不是"测试恰好通过"。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import pandas as pd
import pytest

from Kuantix.core.contracts import Bar

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: easy-tdx 运行期数据目录（**只读**，NF-20）
EASY_TDX_HOME: Path = Path.home() / ".easy_tdx"

#: 上游只读基座源码目录（可能不存在，例如 CI 只装了 wheel）
UPSTREAM_SOURCE_DIR: Path = (
    Path(__file__).resolve().parents[3] / "easy_tdx-main"
)

#: 开启真实网络用例的环境变量
NETWORK_ENV_FLAG: str = "Kuantix_TEST_NETWORK"


# --------------------------------------------------------------------------- #
# pytest 钩子
# --------------------------------------------------------------------------- #


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """离线环境自动跳过 ``network`` 用例。

    Args:
        config: pytest 配置。
        items: 收集到的用例。
    """
    if os.environ.get(NETWORK_ENV_FLAG, "").strip() == "1":
        return
    skip = pytest.mark.skip(
        reason=f"需要外网 TDX 行情服务器；设 {NETWORK_ENV_FLAG}=1 开启"
    )
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


# --------------------------------------------------------------------------- #
# 上游只读守卫
# --------------------------------------------------------------------------- #


def _snapshot(root: Path, *, hash_files: bool) -> dict[str, tuple[int, int, str]]:
    """对目录做一次 ``{相对路径: (size, mtime_ns, sha256)}`` 快照。

    Args:
        root: 目标目录；不存在时返回空字典。
        hash_files: 是否计算内容摘要（大目录建议关闭，只比 size+mtime）。

    Returns:
        快照字典。
    """
    if not root.is_dir():
        return {}
    snap: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        digest = ""
        if hash_files:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snap[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns, digest)
    return snap


def _diff(
    before: dict[str, tuple[int, int, str]],
    after: dict[str, tuple[int, int, str]],
) -> list[str]:
    """比较两次快照，返回人类可读的差异列表。

    Args:
        before: 之前的快照。
        after: 之后的快照。

    Returns:
        差异描述列表；空列表表示完全一致。
    """
    problems: list[str] = []
    for name in sorted(set(after) - set(before)):
        problems.append(f"新增文件: {name}")
    for name in sorted(set(before) - set(after)):
        problems.append(f"文件被删除: {name}")
    for name in sorted(set(before) & set(after)):
        if before[name] != after[name]:
            problems.append(f"文件被修改: {name} {before[name]} -> {after[name]}")
    return problems


@pytest.fixture(scope="session", autouse=True)
def _upstream_readonly_guard() -> Iterator[None]:
    """整个测试 session 期间，上游目录必须字节级不变（NF-1 / NF-20）。

    Yields:
        无。
    """
    home_before = _snapshot(EASY_TDX_HOME, hash_files=True)
    source_before = _snapshot(UPSTREAM_SOURCE_DIR, hash_files=False)
    yield
    home_problems = _diff(home_before, _snapshot(EASY_TDX_HOME, hash_files=True))
    source_problems = _diff(
        source_before, _snapshot(UPSTREAM_SOURCE_DIR, hash_files=False)
    )
    assert not home_problems, (
        f"[红线/NF-20] 测试期间 {EASY_TDX_HOME} 被改动：{home_problems}"
    )
    assert not source_problems, (
        f"[红线/NF-1] 测试期间 {UPSTREAM_SOURCE_DIR} 被改动：{source_problems}"
    )


# --------------------------------------------------------------------------- #
# 通用夹具
# --------------------------------------------------------------------------- #


@pytest.fixture()
def vipdoc_root(tmp_path: Path) -> Path:
    """一次性 vipdoc 根目录（绝不使用 ``~/.Kuantix``）。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        vipdoc 根目录路径。
    """
    root = tmp_path / "vipdoc"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def project_root() -> Path:
    """Kuantix 仓库根目录（静态扫描用）。

    Returns:
        仓库根目录。
    """
    return Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# 数据构造工具
# --------------------------------------------------------------------------- #


def make_bars(
    count: int,
    *,
    start: dt.date = dt.date(2024, 1, 2),
    base_price: float = 10.00,
    price_step: float = 0.01,
    vol_lots: float = 100_000.0,
    amount: float = 1.0e8,
) -> list[Bar]:
    """构造一段合法的日线序列（价格递增、日期严格递增）。

    Args:
        count: 条数。
        start: 起始日期。
        base_price: 起始收盘价。
        price_step: 每日价格增量（保持两位小数，避免 0.01 系数的量化误差）。
        vol_lots: 成交量，单位**手**。
        amount: 成交额，单位元。

    Returns:
        :class:`~Kuantix.core.contracts.Bar` 列表（升序）。
    """
    bars: list[Bar] = []
    day = start
    for i in range(count):
        close = round(base_price + i * price_step, 4)
        low = round(close - price_step, 4)
        high = round(close + price_step, 4)
        bars.append(
            Bar(
                date=day,
                open=close,
                high=high,
                low=low,
                close=close,
                vol=vol_lots,
                amount=amount,
            )
        )
        day = day + dt.timedelta(days=1)
        # 跳过周末，贴近真实交易日序列
        while day.weekday() >= 5:
            day = day + dt.timedelta(days=1)
    return bars


def make_kline_frame(
    count: int,
    *,
    start: dt.date = dt.date(2024, 1, 2),
    base_price: float = 10.00,
    vol_shares: float = 1.0e7,
    amount: float = 1.0e8,
) -> pd.DataFrame:
    """构造一份形如 ``MacClient.get_stock_kline`` 返回的 DataFrame。

    注意 ``vol`` 列单位是**股**（在线口径，RD-8），换算成手是被测代码的职责。

    Args:
        count: 行数。
        start: 起始日期。
        base_price: 起始收盘价。
        vol_shares: 成交量，单位**股**。
        amount: 成交额，单位元。

    Returns:
        含 ``datetime/open/high/low/close/vol/amount`` 列的 DataFrame。
    """
    rows: list[dict[str, Any]] = []
    day = start
    for i in range(count):
        close = round(base_price + i * 0.01, 4)
        rows.append(
            {
                "datetime": pd.Timestamp(day),
                "open": close,
                "high": round(close + 0.01, 4),
                "low": round(close - 0.01, 4),
                "close": close,
                "vol": vol_shares,
                "amount": amount,
            }
        )
        day = day + dt.timedelta(days=1)
        while day.weekday() >= 5:
            day = day + dt.timedelta(days=1)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# RD-10 连接陷阱模拟器
# --------------------------------------------------------------------------- #


@dataclass
class ClientCallLog:
    """单个替身客户端的调用记录。

    Attributes:
        index: 该客户端是工厂创建的第几个（从 0 起）。
        list_calls: ``get_security_list`` 被调用次数。
        count_calls: ``get_security_count`` 被调用次数。
        closed: 是否已被调用方关闭。
        penalised: 是否触发过"连接复用惩罚"。
    """

    index: int
    list_calls: int = 0
    count_calls: int = 0
    closed: bool = False
    penalised: bool = False


class FakeTdxClient:
    """``TdxClient`` 的离线替身，**内建 RD-10 连接复用陷阱**。

    真实服务端对同一条连接的第 2 次 ``get_security_list`` 不响应，客户端要等
    15s read timeout。这里用 ``reuse_penalty_seconds`` 模拟该行为（默认 2s，
    足以让"每页 <1s"的验收线失败，又不至于让测试跑太久）。
    """

    def __init__(
        self,
        *,
        log: ClientCallLog,
        rows_by_market: dict[int, list[dict[str, str]]],
        page_size: int,
        fresh_latency_seconds: float,
        reuse_penalty_seconds: float,
    ) -> None:
        """初始化替身。

        Args:
            log: 本连接的调用记录（由工厂持有，测试可直接断言）。
            rows_by_market: ``{market_int: [{"code":..., "name":...}, ...]}``。
            page_size: 每页条数。
            fresh_latency_seconds: 新连接首次请求的模拟耗时。
            reuse_penalty_seconds: 同一连接重复请求的惩罚耗时（模拟 15s 超时）。
        """
        self._log = log
        self._rows = rows_by_market
        self._page_size = int(page_size)
        self._fresh = float(fresh_latency_seconds)
        self._penalty = float(reuse_penalty_seconds)

    def get_security_count(self, market: Any) -> int:
        """返回某市场证券总数。

        Args:
            market: 上游 ``Market`` 枚举或其整数值。

        Returns:
            总数。
        """
        self._log.count_calls += 1
        time.sleep(self._fresh)
        return len(self._rows.get(int(market), []))

    def get_security_list(self, market: Any, start: int) -> pd.DataFrame:
        """返回一页证券清单。

        Args:
            market: 上游 ``Market`` 枚举或其整数值。
            start: 分页偏移。

        Returns:
            含 ``code``/``name`` 列的 DataFrame。

        Raises:
            AssertionError: 连接已关闭仍被调用（说明调用方生命周期管理有误）。
        """
        assert not self._log.closed, "连接关闭后仍被调用，生命周期管理有误"
        if self._log.list_calls >= 1:
            # === RD-10 陷阱：同一条连接的第 2 次请求，服务端不响应 ===
            self._log.penalised = True
            time.sleep(self._penalty)
        else:
            time.sleep(self._fresh)
        self._log.list_calls += 1
        rows = self._rows.get(int(market), [])
        page = rows[int(start) : int(start) + self._page_size]
        return pd.DataFrame(page, columns=["code", "name"])

    def close(self) -> None:
        """关闭连接（幂等）。"""
        self._log.closed = True


@dataclass
class FakeClientFactory:
    """``TdxClientFactory`` 的鸭子类型替身（只实现枚举器用到的接口）。

    Attributes:
        rows_by_market: 各市场的证券清单原始行。
        page_size: 每页条数。
        fresh_latency_seconds: 新连接首次请求耗时。
        reuse_penalty_seconds: 连接复用惩罚耗时。
        logs: 所有已创建连接的调用记录。
    """

    rows_by_market: dict[int, list[dict[str, str]]]
    page_size: int = 1000
    fresh_latency_seconds: float = 0.002
    reuse_penalty_seconds: float = 2.0
    logs: list[ClientCallLog] = field(default_factory=list)

    @property
    def created_total(self) -> int:
        """已创建的连接总数（枚举器用它统计 RD-10 是否生效）。"""
        return len(self.logs)

    def new_tdx_client(self, host: str | None = None) -> FakeTdxClient:
        """创建一条**全新**替身连接。

        Args:
            host: 忽略（替身不做网络连接）。

        Returns:
            新的 :class:`FakeTdxClient`。
        """
        log = ClientCallLog(index=len(self.logs))
        self.logs.append(log)
        return FakeTdxClient(
            log=log,
            rows_by_market=self.rows_by_market,
            page_size=self.page_size,
            fresh_latency_seconds=self.fresh_latency_seconds,
            reuse_penalty_seconds=self.reuse_penalty_seconds,
        )


class ReusingClientFactory(FakeClientFactory):
    """**故意违规**的工厂：永远返回同一条连接（用于自检测试有效性）。

    这个类的存在是为了证明验收④ 的测试**真的能抓到回归**：如果
    ``UniverseEnumerator`` 改成复用连接，测试必须失败。
    """

    _shared: FakeTdxClient | None = None

    def new_tdx_client(self, host: str | None = None) -> FakeTdxClient:
        """返回共享的同一条连接（模拟连接复用的错误实现）。

        Args:
            host: 忽略。

        Returns:
            共享的 :class:`FakeTdxClient`。
        """
        if self._shared is None:
            self._shared = super().new_tdx_client(host)
        # 复用时把 closed 复位，模拟"上层以为关了、其实底层连接还在"
        self._shared._log.closed = False  # noqa: SLF001 - 替身自省，测试专用
        return self._shared


def build_rows(codes: Sequence[str], *, prefix: str = "标的") -> list[dict[str, str]]:
    """把代码列表包装成 ``get_security_list`` 的行结构。

    Args:
        codes: 证券代码列表。
        prefix: 名称前缀。

    Returns:
        行字典列表。
    """
    return [{"code": c, "name": f"{prefix}{c}"} for c in codes]
