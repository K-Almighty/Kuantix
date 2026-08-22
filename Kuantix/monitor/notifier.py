"""Notifier —— 多通道并发投递告警（NF-2 插件化 / NF-26 不静默）。

设计
----
- :class:`NotifyChannel` 是推送通道插件抽象基类，P0 实现两个：
    * :class:`Kuantix.monitor.channels.desktop.DesktopChannel`（macOS 桌面通知）；
    * :class:`Kuantix.monitor.channels.webhook.WebhookChannel`（POST JSON）。
- :class:`Notifier.send(alert)` 把告警**并发**分发到全部启用通道，
  任一失败**显式记录**（返回 per-channel 布尔明细，不静默吞）；
- 通道列表从配置 + 插件发现加载（``discover_plugins("Kuantix.monitor.channels")``）。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

from Kuantix.core.contracts import Alert
from Kuantix.core.fail_loud import MissingConfigError, require_non_empty

from Kuantix.core.plugins import (
    REGISTRY,
    PluginKind,
    PluginRegistry,
    discover_plugins,
)

__all__ = [
    "NotifyChannel",
    "Notifier",
    "CHANNELS_PACKAGE",
]

logger = logging.getLogger(__name__)

#: 通道插件所在包（插件发现入口）
CHANNELS_PACKAGE = "Kuantix.monitor.channels"


class NotifyChannel(ABC):
    """推送通道插件抽象基类。

    子类需定义 ``name`` / ``display_name`` 并实现 :meth:`send`。
    注册方式：``@register_plugin(PluginKind.NOTIFY_CHANNEL, "desktop")``。
    """

    #: 通道插件名（注册名）
    name: str = ""
    #: 展示名（M16 ChannelInfo）
    display_name: str = ""

    @abstractmethod
    def send(self, alert: Alert) -> bool:
        """投递一条告警；返回是否成功。

        失败必须**显式记录日志并返回 False**（NF-26），不得抛给上层静默吞。
        """

    def info(self) -> dict[str, Any]:
        """返回 ChannelInfo（契约 §3.5，M16 端点）。"""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "enabled": True,
            "healthy": None,
        }


class Notifier:
    """告警分发器。

    Args:
        channels: 显式通道列表；``None`` 时调用 :meth:`load_channels` 加载。
        registry: 插件表；``None`` 使用全局 REGISTRY。
        max_workers: 并发线程数。
    """

    def __init__(
        self,
        channels: Iterable[NotifyChannel] | None = None,
        *,
        registry: PluginRegistry | None = None,
        max_workers: int = 4,
    ) -> None:
        self._registry = registry if registry is not None else REGISTRY
        self._channels: list[NotifyChannel] = (
            list(channels) if channels is not None else self.load_channels()
        )
        if max_workers <= 0:
            raise MissingConfigError(
                f"[fail-loud/NF-26] Notifier max_workers 必须为正，实际 {max_workers!r}"
            )
        self._max_workers = int(max_workers)

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #

    @classmethod
    def load_channels(
        cls,
        *,
        registry: PluginRegistry | None = None,
    ) -> list[NotifyChannel]:
        """从插件发现 + 全局注册表加载全部通道。

        通道插件放在 ``Kuantix/monitor/channels/`` 下（``@register_plugin``
        自动注册），本方法触发发现后按注册名实例化。加载失败显式抛
        :class:`PluginLoadError`（NF-26，不静默跳过坏插件）。
        """
        target = registry if registry is not None else REGISTRY
        discover_plugins(CHANNELS_PACKAGE)
        channels: list[NotifyChannel] = []
        for name in target.names(PluginKind.NOTIFY_CHANNEL):
            cls_ = target.resolve(PluginKind.NOTIFY_CHANNEL, name)
            try:
                instance = cls_()
            except Exception as exc:  # noqa: BLE001 - 实例化失败显式报错
                raise MissingConfigError(
                    f"[fail-loud/NF-26] 推送通道 {name!r} 实例化失败: {exc}"
                ) from exc
            channels.append(instance)
        return channels

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    @property
    def channels(self) -> list[NotifyChannel]:
        """返回当前通道列表。"""
        return list(self._channels)

    def channels_info(self) -> list[dict[str, Any]]:
        """返回各通道 ChannelInfo（M16）。"""
        return [channel.info() for channel in self._channels]

    # ------------------------------------------------------------------ #
    # 投递
    # ------------------------------------------------------------------ #

    def send(self, alert: Alert) -> dict[str, bool]:
        """并发分发告警到全部启用通道。

        Args:
            alert: 告警。

        Returns:
            ``{channel_name: bool}`` —— 每个通道的投递结果。

        Notes:
            任一通道失败不会中断其余通道（隔离），失败明细写入日志（不静默）。
        """
        require_non_empty(self._channels, "推送通道列表")
        results: dict[str, bool] = {}

        def _dispatch(channel: NotifyChannel) -> tuple[str, bool]:
            try:
                ok = channel.send(alert)
            except Exception as exc:  # noqa: BLE001 - 通道自身异常也记失败，不静默
                logger.error("推送通道 %s 抛异常: %s", channel.name, exc)
                return channel.name, False
            return channel.name, bool(ok)

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = [executor.submit(_dispatch, ch) for ch in self._channels]
            for future in futures:
                name, ok = future.result()
                results[name] = ok
                if not ok:
                    logger.error("推送通道 %s 投递失败（告警 %s）", name, alert.id)
        return results
