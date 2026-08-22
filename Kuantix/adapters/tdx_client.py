"""easy-tdx 客户端工厂（NF-1 / NF-20 / RD-10 / 陷阱 T5 的落地点）。

三条独立链路（陷阱 T5）
----------------------
上游把「A 股」和「扩展市场（港股/美股/期货）」拆成了两套**互不兼容**的协议，
端口、握手方式、K 线方法名全都不同。混用会直接连不上或拿到空数据：

===========  ==================  ======  ==================  ====================
用途          客户端类             端口     K 线方法             市场码
===========  ==================  ======  ==================  ====================
A 股行情      ``MacClient``       7709    ``get_stock_kline``  ``Market.SH/SZ`` (1/0)
A 股证券清单  ``TdxClient``       7709    ``get_security_list`` ``Market.SH/SZ``
港股/美股     ``MacExClient``     7727    ``goods_kline``      ``ExMarket`` (31/74)
===========  ==================  ======  ==================  ====================

``MacExClient`` 还需要 Login 握手（``connect()`` 内部自动做），
与 7709 的链路**完全独立**，不能共用连接。

红线约束
--------
- **禁止 ``from_best_host()``**（NF-1/NF-20）：上游三个客户端的
  ``from_best_host`` 都会调用 ``save_best_*_host()`` 写 ``~/.easy_tdx/config.json``。
  Kuantix 对上游目录只读，因此本模块**只用显式 host/port 构造**。
- **禁止 host/port/timeout 传 None**：上游 ``TdxClient.__init__`` 有个已知缺陷
  （``client.py:237``）——它把 ``TdxConnection(host, port, timeout)`` 建在**原始入参**
  上而非解析后的 ``self._host``，传 None 会连到 None。故本模块强制显式值。
- **RD-10 每页新连接**：``get_security_list`` 复用同一连接时，第 2 页起每页
  ≈15.2s（S2 实测），新建连接每页 ≈0.03～0.05s。因此提供
  :meth:`TdxClientFactory.new_tdx_client`（**不入池**）供枚举逐页使用。
- **NF-28 资源隔离**：监控链路与回补链路不得共享连接，
  用 :meth:`TdxClientFactory.new_mac_client` 取独立连接。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from easy_tdx.client import TdxClient
from easy_tdx.ex.mac_client import MacExClient
from easy_tdx.mac.client import MacClient

from Kuantix.adapters.known_hosts import (
    EASY_TDX_CONFIG_PATH,
    FileFingerprint,
    HostBook,
    assert_untouched,
    build_host_book,
)
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    MissingConfigError,
    require_non_empty,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期需要
    from Kuantix.config import Config

__all__ = [
    "ClientKind",
    "ClientSpec",
    "TdxClientFactory",
    "FORBIDDEN_UPSTREAM_FACTORIES",
]

#: 上游中会写 ``~/.easy_tdx/`` 的工厂方法，Kuantix 全库禁止调用（NF-1/NF-20）。
#: 有静态测试 ``test_t02_redlines.py`` 守这条线。
FORBIDDEN_UPSTREAM_FACTORIES: tuple[str, ...] = (
    "from_best_host",
    "save_best_host",
    "save_best_mac_host",
    "save_best_mac_ex_host",
    "detect_tdx_home",
    "get_security_list_all",  # 会把结果缓存写进 ~/.easy_tdx/cache
)


class ClientKind(str, Enum):
    """客户端类型（对应三条独立链路，陷阱 T5）。"""

    #: A 股在线行情（7709，``get_stock_kline``）
    MAC = "mac"
    #: A 股证券清单 / 标准协议（7709，``get_security_list``）
    STD = "std"
    #: 扩展市场港股/美股（7727，``goods_kline``，需 Login 握手）
    MAC_EX = "mac_ex"


@dataclass(frozen=True)
class ClientSpec:
    """一个客户端连接的完整规格（全部显式，无 None）。

    Attributes:
        kind: 客户端类型。
        host: 服务器 IP/域名。
        port: 端口（``MAC``/``STD`` 为 7709，``MAC_EX`` 为 7727）。
        timeout: socket 超时秒数。
    """

    kind: ClientKind
    host: str
    port: int
    timeout: float

    def __post_init__(self) -> None:
        if not str(self.host).strip():
            raise MissingConfigError(
                f"[fail-loud/NF-1] {self.kind.value} 客户端 host 为空："
                f"Kuantix 禁止 from_best_host 自动优选（会写 ~/.easy_tdx），"
                f"必须在 config.toml 的 [tdx] 段显式配置主机"
            )
        if not isinstance(self.port, int) or self.port <= 0 or self.port > 65535:
            raise MissingConfigError(
                f"[fail-loud/NF-1] {self.kind.value} 客户端 port 非法：{self.port!r}"
            )
        if not (self.timeout > 0):
            raise MissingConfigError(
                f"[fail-loud/NF-1] {self.kind.value} 客户端 timeout 必须为正数，"
                f"实际 {self.timeout!r}"
            )

    @property
    def key(self) -> tuple[str, str, int]:
        """连接池键。"""
        return (self.kind.value, self.host, self.port)

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "kind": self.kind.value,
            "host": self.host,
            "port": self.port,
            "timeout": self.timeout,
        }


class TdxClientFactory:
    """easy-tdx 客户端工厂 + 进程内连接池。

    这是**全项目唯一**创建 easy-tdx 客户端的地方（NF-1）。任何其它模块想拿
    连接都必须经过本工厂，从而集中保证：显式 host/port、禁 ``from_best_host``、
    不写上游目录、RD-10 的"每页新连接"能力可用。

    池化策略：
        - ``get_*_client()``：按 ``(kind, host, port)`` 复用**进程内单例**，
          适合"一条长连接反复拉 K 线"（S2 实测复用连接拉 K 线 0.06s/只，
          每只新建连接反而退化到 0.53s/只）。
        - ``new_*_client()``：**不入池**，每次返回全新连接。用于
          ① 证券清单逐页枚举（RD-10）；② 监控链路资源隔离（NF-28）；
          ③ 多线程并发 worker（连接非线程安全）。

    Examples:
        >>> factory = TdxClientFactory.from_config()          # doctest: +SKIP
        >>> mac = factory.get_mac_client()                    # doctest: +SKIP
        >>> df = mac.get_stock_kline(market=1, code="600000") # doctest: +SKIP
    """

    def __init__(
        self,
        host_book: HostBook,
        *,
        auto_reconnect: bool = True,
        heartbeat_interval: float = 15.0,
    ) -> None:
        """初始化工厂。

        Args:
            host_book: 主机簿（由 :func:`Kuantix.adapters.known_hosts.build_host_book`
                构造，已完成 config.toml 与上游 known_hosts 的只读合并）。
            auto_reconnect: 断线是否自动重连。
            heartbeat_interval: 心跳间隔秒数；``<=0`` 关闭心跳。
                ``MacExClient`` 不支持心跳参数，该值对其无效。

        Raises:
            MissingConfigError: 主机簿为空。
        """
        self._book = host_book
        self._auto_reconnect = bool(auto_reconnect)
        self._heartbeat_interval = float(heartbeat_interval)
        self._pool: dict[tuple[str, str, int], Any] = {}
        self._lock = threading.RLock()
        self._created_total = 0

    # ------------------------------------------------------------------ #
    # 构造入口
    # ------------------------------------------------------------------ #

    @classmethod
    def from_config(cls, config: "Config | None" = None) -> "TdxClientFactory":
        """从 :class:`Kuantix.config.Config` 构造工厂。

        Args:
            config: 配置对象；``None`` 时取全局单例
                :func:`Kuantix.config.get_config`。

        Returns:
            工厂实例。

        Raises:
            MissingConfigError: ``[tdx]`` 段主机列表为空。
        """
        if config is None:
            from Kuantix.config import get_config

            config = get_config()
        tdx = config.tdx
        book = build_host_book(
            std_hosts=tdx.std_hosts,
            mac_hosts=tdx.mac_hosts,
            mac_ex_hosts=tdx.mac_ex_hosts,
            port=tdx.port,
            ex_port=tdx.ex_port,
            timeout_seconds=tdx.timeout_seconds,
            use_easy_tdx_known_hosts=tdx.use_easy_tdx_known_hosts,
        )
        return cls(book)

    # ------------------------------------------------------------------ #
    # 规格解析
    # ------------------------------------------------------------------ #

    @property
    def host_book(self) -> HostBook:
        """返回主机簿（只读）。"""
        return self._book

    def spec_for(self, kind: ClientKind | str, host: str | None = None) -> ClientSpec:
        """解析某类客户端的连接规格。

        Args:
            kind: 客户端类型。
            host: 显式指定主机；``None`` 时取主机簿中该类型的首选主机。

        Returns:
            :class:`ClientSpec`。

        Raises:
            MissingConfigError: 类型未知或该类型无可用主机。
        """
        resolved_kind = kind if isinstance(kind, ClientKind) else ClientKind(str(kind))
        if resolved_kind is ClientKind.MAC:
            chosen = host if host is not None else self._book.primary_mac_host
            port = self._book.port
        elif resolved_kind is ClientKind.STD:
            chosen = host if host is not None else self._book.primary_std_host
            port = self._book.port
        elif resolved_kind is ClientKind.MAC_EX:
            chosen = host if host is not None else self._book.primary_mac_ex_host
            port = self._book.ex_port
        else:  # pragma: no cover - Enum 已穷举
            raise MissingConfigError(f"[fail-loud/NF-1] 未知客户端类型 {kind!r}")
        return ClientSpec(
            kind=resolved_kind,
            host=str(chosen),
            port=int(port),
            timeout=float(self._book.timeout_seconds),
        )

    # ------------------------------------------------------------------ #
    # 池化客户端（进程内单例）
    # ------------------------------------------------------------------ #

    def get_mac_client(self, host: str | None = None) -> MacClient:
        """获取（或复用）A 股行情客户端 ``MacClient``（7709）。

        Args:
            host: 显式主机；``None`` 用主机簿首选。

        Returns:
            池化的 :class:`~easy_tdx.mac.client.MacClient`。
        """
        return self._pooled(self.spec_for(ClientKind.MAC, host))

    def get_tdx_client(self, host: str | None = None) -> TdxClient:
        """获取（或复用）标准客户端 ``TdxClient``（7709，证券清单用）。

        .. warning::
            **不要**用它逐页拉 ``get_security_list``——同一连接第 2 页起会
            卡满超时（RD-10，S2 实测 15.2s/页）。逐页枚举请用
            :meth:`new_tdx_client`。

        Args:
            host: 显式主机；``None`` 用主机簿首选。

        Returns:
            池化的 :class:`~easy_tdx.client.TdxClient`。
        """
        return self._pooled(self.spec_for(ClientKind.STD, host))

    def get_mac_ex_client(self, host: str | None = None) -> MacExClient:
        """获取（或复用）扩展市场客户端 ``MacExClient``（7727，港股/美股）。

        Args:
            host: 显式主机；``None`` 用主机簿首选。

        Returns:
            池化的 :class:`~easy_tdx.ex.mac_client.MacExClient`。
        """
        return self._pooled(self.spec_for(ClientKind.MAC_EX, host))

    # ------------------------------------------------------------------ #
    # 非池化客户端（RD-10 / NF-28）
    # ------------------------------------------------------------------ #

    def new_mac_client(self, host: str | None = None) -> MacClient:
        """新建**独立**的 ``MacClient``（不入池）。

        用于监控链路（NF-28 资源隔离）与并发 worker（连接非线程安全）。
        调用方负责 ``close()``。

        Args:
            host: 显式主机；``None`` 用主机簿首选。

        Returns:
            全新的 :class:`~easy_tdx.mac.client.MacClient`。
        """
        return self._construct(self.spec_for(ClientKind.MAC, host))

    def new_tdx_client(self, host: str | None = None) -> TdxClient:
        """新建**独立**的 ``TdxClient``（不入池）——RD-10 每页新连接的入口。

        Args:
            host: 显式主机；``None`` 用主机簿首选。

        Returns:
            全新的 :class:`~easy_tdx.client.TdxClient`；调用方负责 ``close()``。
        """
        return self._construct(self.spec_for(ClientKind.STD, host))

    def new_mac_ex_client(self, host: str | None = None) -> MacExClient:
        """新建**独立**的 ``MacExClient``（不入池）。

        Args:
            host: 显式主机；``None`` 用主机簿首选。

        Returns:
            全新的 :class:`~easy_tdx.ex.mac_client.MacExClient`。
        """
        return self._construct(self.spec_for(ClientKind.MAC_EX, host))

    # ------------------------------------------------------------------ #
    # 连通性测试（E2，只测不写）
    # ------------------------------------------------------------------ #

    def probe_connection(
        self,
        kind: ClientKind | str,
        *,
        host: str,
        port: int,
        timeout: float,
    ) -> dict[str, Any]:
        """**只测不写**的连通性测试：新建连接 → connect → 测延迟 → close。

        Args:
            kind: 客户端类型（``std`` / ``mac`` / ``mac_ex``）。
            host: 显式服务器 IP/域名。
            port: 显式端口（``std``/``mac`` 为 7709，``mac_ex`` 为 7727）。
            timeout: socket 超时秒数（E2 用短超时快速失败）。

        Returns:
            ``{"ok": bool, "latency_ms": int|None, "error": str|None}``：
            ``ok=True`` 时 ``latency_ms`` 为 connect 握手耗时（毫秒）；
            ``ok=False`` 时 ``error`` 为人类可读错误。**这是业务结果**——
            调用方应返回 ``code=0`` 信封而非 HTTP 错误（fail-loud 体现在
            ``error`` 字段）。

        Raises:
            MissingConfigError: kind 非法 / host 为空 / port 越界 /
                timeout 非正（由 :class:`ClientSpec` 校验）。

        NF-20 保证：
            - 经 :meth:`_construct` 用**显式 host/port** 新建连接（不入池）；
            - 禁 from_best_host / save_best_*，不落盘任何 best host；
            - ``finally`` 中 :meth:`_safe_close` 关闭连接（清理失败只留痕）。
        """
        try:
            resolved_kind = kind if isinstance(kind, ClientKind) else ClientKind(str(kind))
        except ValueError as exc:
            raise MissingConfigError(
                f"[fail-loud/NF-1] 未知客户端类型 {kind!r}（期望 std/mac/mac_ex）"
            ) from exc
        spec = ClientSpec(
            kind=resolved_kind,
            host=str(host).strip(),
            port=int(port),
            timeout=float(timeout),
        )
        client = self._construct(spec)  # 新建连接，不入池
        ok = False
        latency_ms: int | None = None
        error: str | None = None
        try:
            started = time.perf_counter()
            client.connect()
            latency_ms = int(round((time.perf_counter() - started) * 1000))
            ok = True
        except Exception as exc:  # noqa: BLE001 - 业务结果：连接失败不是程序缺陷
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self._safe_close(client)
        return {"ok": ok, "latency_ms": latency_ms, "error": error}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def close(self, kind: ClientKind | str, host: str | None = None) -> bool:
        """关闭并移除池中某个客户端。

        Args:
            kind: 客户端类型。
            host: 显式主机；``None`` 用主机簿首选。

        Returns:
            ``True`` 表示确实关闭了一个连接。
        """
        spec = self.spec_for(kind, host)
        with self._lock:
            client = self._pool.pop(spec.key, None)
        if client is None:
            return False
        self._safe_close(client)
        return True

    def close_all(self) -> int:
        """关闭池中所有客户端。

        Returns:
            关闭的连接数。
        """
        with self._lock:
            clients = list(self._pool.values())
            self._pool.clear()
        for client in clients:
            self._safe_close(client)
        return len(clients)

    def __enter__(self) -> "TdxClientFactory":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close_all()

    # ------------------------------------------------------------------ #
    # 诊断
    # ------------------------------------------------------------------ #

    def pooled_keys(self) -> list[tuple[str, str, int]]:
        """返回当前池中的连接键列表（诊断用）。"""
        with self._lock:
            return sorted(self._pool)

    @property
    def created_total(self) -> int:
        """本工厂累计创建过的连接数（含非池化）。测试用它验证 RD-10。"""
        return self._created_total

    def assert_upstream_config_untouched(self) -> None:
        """断言 ``~/.easy_tdx/config.json`` 自主机簿构建以来未被改动（NF-20）。

        Raises:
            DataIntegrityError: 上游配置文件被修改（说明某处误调了
                ``save_best_*_host`` / ``from_best_host``）。
        """
        before: FileFingerprint | None = self._book.upstream_fingerprint
        if before is None:
            # 主机簿未读取过上游配置（use_easy_tdx_known_hosts=false）：
            # 此时我们与该文件毫无交集，无基线可比，直接视为未触碰。
            return
        assert_untouched(before)

    def describe(self) -> dict[str, Any]:
        """返回工厂状态摘要（JSON 安全，用于 ``/health`` 与诊断）。"""
        return {
            "upstream_config": str(EASY_TDX_CONFIG_PATH),
            "host_book": self._book.to_dict(),
            "pooled": [list(k) for k in self.pooled_keys()],
            "created_total": self._created_total,
            "auto_reconnect": self._auto_reconnect,
            "heartbeat_interval": self._heartbeat_interval,
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _pooled(self, spec: ClientSpec) -> Any:
        """按规格取池化客户端，不存在则创建。"""
        with self._lock:
            existing = self._pool.get(spec.key)
            if existing is not None:
                return existing
            client = self._construct(spec)
            self._pool[spec.key] = client
            return client

    def _construct(self, spec: ClientSpec) -> Any:
        """按规格构造一个全新客户端（**始终显式传 host/port/timeout**）。

        Args:
            spec: 连接规格。

        Returns:
            上游客户端实例。

        Raises:
            DataIntegrityError: 上游构造出的客户端主机与我们要求的不一致
                （说明上游内部又走了自动优选，必须立即暴露）。
        """
        if spec.kind is ClientKind.MAC:
            client: Any = MacClient(
                host=spec.host,
                port=spec.port,
                timeout=spec.timeout,
                auto_reconnect=self._auto_reconnect,
                heartbeat_interval=self._heartbeat_interval,
            )
        elif spec.kind is ClientKind.STD:
            client = TdxClient(
                host=spec.host,
                port=spec.port,
                timeout=spec.timeout,
                auto_reconnect=self._auto_reconnect,
                heartbeat_interval=self._heartbeat_interval,
            )
        else:
            # MacExClient 无 heartbeat_interval 参数（上游 ex/mac_client.py:78）
            client = MacExClient(
                host=spec.host,
                port=spec.port,
                timeout=spec.timeout,
                auto_reconnect=self._auto_reconnect,
            )
        self._assert_host_honoured(client, spec)
        with self._lock:
            self._created_total += 1
        return client

    @staticmethod
    def _assert_host_honoured(client: Any, spec: ClientSpec) -> None:
        """校验上游确实用了我们给的 host/port（防止静默走自动优选）。

        Args:
            client: 刚构造的上游客户端。
            spec: 期望的规格。

        Raises:
            DataIntegrityError: 实际 host/port 与期望不一致。
        """
        actual_host = getattr(client, "_host", spec.host)
        actual_port = getattr(client, "_port", spec.port)
        if str(actual_host) != spec.host or int(actual_port) != spec.port:
            raise DataIntegrityError(
                f"[fail-loud/NF-1] 上游 {spec.kind.value} 客户端未采用显式主机："
                f"期望 {spec.host}:{spec.port}，实际 {actual_host}:{actual_port}。"
                f"疑似走了 from_best_host 自动优选（会写 ~/.easy_tdx，违反 NF-20）"
            )

    @staticmethod
    def _safe_close(client: Any) -> None:
        """关闭客户端。

        关闭失败只记录不抛出：连接清理失败不应掩盖上层的真实错误，
        但也**不允许静默吞掉**——异常信息会挂到 ``client._Kuantix_close_error``
        供诊断读取（NF-26 的折中：清理路径允许降级，但必须留痕）。
        """
        closer = getattr(client, "close", None)
        if closer is None:
            return
        try:
            closer()
        except Exception as exc:  # noqa: BLE001 - 清理路径留痕而非静默
            setattr(client, "_Kuantix_close_error", f"{type(exc).__name__}: {exc}")


def _unused_guard() -> tuple[str, ...]:
    """保持 :data:`FORBIDDEN_UPSTREAM_FACTORIES` 被引用，便于静态检查定位。"""
    return FORBIDDEN_UPSTREAM_FACTORIES


require_non_empty(FORBIDDEN_UPSTREAM_FACTORIES, "禁用上游工厂方法清单")
