"""WebhookChannel —— 通用 Webhook 推送（P0）。

实现
----
- ``urllib.request`` POST JSON 到配置的 webhook URL（**零第三方依赖**）；
- 超时 + 重试（默认 3 次，指数退避），失败记录日志并返回 ``False``（NF-26，
  不静默）；
- 载荷：``{"alert": alert.to_dict()}``（契约 §3.5 Alert 字典）。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from Kuantix.core.contracts import Alert
from Kuantix.core.fail_loud import MissingConfigError, require_finite
from Kuantix.core.plugins import PluginKind, register_plugin

from Kuantix.monitor.notifier import NotifyChannel

__all__ = ["WebhookChannel"]

logger = logging.getLogger(__name__)


@register_plugin(PluginKind.NOTIFY_CHANNEL, "webhook")
class WebhookChannel(NotifyChannel):
    """通用 Webhook 通道。

    Args:
        url: 回调 URL（必须非空，fail-loud）。
        timeout_seconds: 单次请求超时（秒）。
        retry_attempts: 最大重试次数（含首次）。
        retry_backoff_seconds: 退避初始秒数（指数增长）。
    """

    name = "webhook"
    display_name = "Webhook 回调"

    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 5.0,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        if not str(url).strip():
            raise MissingConfigError(
                "[fail-loud/NF-26] WebhookChannel url 不能为空（R4 禁 .get 兜底）"
            )
        self._url = str(url).strip()
        self._timeout = require_finite(timeout_seconds, "webhook.timeout_seconds")
        self._retry_attempts = int(retry_attempts)
        if self._retry_attempts <= 0:
            raise MissingConfigError(
                f"[fail-loud/NF-26] WebhookChannel retry_attempts 必须为正，"
                f"实际 {retry_attempts!r}"
            )
        self._retry_backoff = require_finite(
            retry_backoff_seconds, "webhook.retry_backoff_seconds"
        )

    def send(self, alert: Alert) -> bool:
        """POST 告警 JSON 到 webhook。

        Returns:
            是否成功（2xx 响应）。

        Notes:
            网络失败 / 超时 / 非 2xx 都记录日志并重试，重试耗尽返回 ``False``。
        """
        payload = {"alert": alert.to_dict()}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        last_error: Exception | None = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    status = response.status
                if 200 <= status < 300:
                    return True
                logger.error(
                    "webhook 返回非 2xx status=%s（第 %s/%s 次）url=%s",
                    status,
                    attempt,
                    self._retry_attempts,
                    self._url,
                )
            except urllib.error.HTTPError as exc:
                last_error = exc
                logger.error(
                    "webhook HTTPError %s（第 %s/%s 次）url=%s", exc.code, attempt, self._retry_attempts, self._url
                )
            except Exception as exc:  # noqa: BLE001 - 显式记录 + 重试，不静默
                last_error = exc
                logger.error(
                    "webhook 请求失败（第 %s/%s 次）url=%s: %s",
                    attempt,
                    self._retry_attempts,
                    self._url,
                    exc,
                )
            if attempt < self._retry_attempts:
                time.sleep(self._retry_backoff * (2 ** (attempt - 1)))
        detail = f": {last_error}" if last_error is not None else ""
        logger.error(
            "webhook 重试 %s 次仍失败 url=%s%s",
            self._retry_attempts,
            self._url,
            detail,
        )
        return False
