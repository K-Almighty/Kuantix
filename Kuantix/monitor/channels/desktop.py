"""DesktopChannel —— macOS 桌面通知（P0）。

实现
----
- 用标准库 ``osascript``（``subprocess``）发 macOS 通知，**不引入第三方依赖**
  （不用 pync / plyer）；
- 失败不吞：记录日志并返回 ``False``（NF-26）。
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from Kuantix.core.contracts import Alert
from Kuantix.core.fail_loud import MissingConfigError
from Kuantix.core.plugins import PluginKind, register_plugin

from Kuantix.monitor.notifier import NotifyChannel

__all__ = ["DesktopChannel"]

logger = logging.getLogger(__name__)


@register_plugin(PluginKind.NOTIFY_CHANNEL, "desktop")
class DesktopChannel(NotifyChannel):
    """macOS 桌面通知通道。

    Args:
        app_name: 通知显示的应用名。
    """

    name = "desktop"
    display_name = "桌面通知"

    def __init__(self, app_name: str = "Kuantix") -> None:
        if not str(app_name).strip():
            raise MissingConfigError(
                "[fail-loud/NF-26] DesktopChannel app_name 不能为空"
            )
        self._app_name = str(app_name).strip()

    def send(self, alert: Alert) -> bool:
        """通过 osascript 发送桌面通知。

        Returns:
            是否成功（osascript 返回码 0）。

        Notes:
            任何失败（osascript 不存在 / 返回非零 / 超时）都记录日志并返回
            ``False``，绝不静默。
        """
        title = f"[{alert.level.value.upper()}] {alert.code} {alert.rule}"
        body = self._sanitize(alert.message)
        script = (
            f'display notification "{body}" with title "{self._sanitize(title)}" '
            f'subtitle "{self._app_name}"'
        )
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except FileNotFoundError:
            logger.error("桌面通知失败：未找到 osascript（非 macOS 环境？）")
            return False
        except subprocess.TimeoutExpired:
            logger.error("桌面通知失败：osascript 超时")
            return False
        except Exception as exc:  # noqa: BLE001 - 显式记录，不静默
            logger.error("桌面通知失败: %s", exc)
            return False

        if proc.returncode != 0:
            logger.error(
                "桌面通知失败 rc=%s stderr=%s", proc.returncode, proc.stderr.strip()
            )
            return False
        return True

    @staticmethod
    def _sanitize(text: str) -> str:
        """去掉会破坏 AppleScript 字符串的字面量字符。"""
        return text.replace('"', "'").replace("\\", " ")

    def info(self) -> dict[str, Any]:
        info = super().info()
        info["app_name"] = self._app_name
        return info
