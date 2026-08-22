"""监控推送通道插件包（NF-2 自动发现目录）。

P0 通道：
- :class:`DesktopChannel` —— macOS 桌面通知（osascript，标准库，零第三方依赖）；
- :class:`WebhookChannel` —— 通用 Webhook（POST JSON，urllib，超时 + 重试）。

两个通道均经 ``@register_plugin(PluginKind.NOTIFY_CHANNEL, ...)`` 注册到全局
插件表；:meth:`Notifier.load_channels` 发现本包后按注册名实例化。
"""

from __future__ import annotations

from Kuantix.monitor.channels.desktop import DesktopChannel
from Kuantix.monitor.channels.webhook import WebhookChannel

__all__ = ["DesktopChannel", "WebhookChannel"]
