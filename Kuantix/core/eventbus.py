"""进程内事件总线（NF-3 解耦）。

监控告警经本总线分发，让规则引擎与推送通道互不 import。

fail-loud 要点（NF-26）：
- 订阅者抛异常时**不吞掉**：记 ``logger.exception`` + 计入
  :class:`DeliveryReport.failures`，并在 ``raise_on_error=True`` 时
  聚合抛 :class:`EventDeliveryError`；
- 一个订阅者失败不影响其余订阅者投递（隔离而非静默）。

主题注册表（team-lead 裁决 1 / 方案 C）：
- 未声明的主题在 ``publish`` 与 ``subscribe`` **两侧**都过
  :func:`require_known` 校验，拼错主题名立刻报错 —— 避免告警 publish 到
  拼错的 topic 后无声蒸发（NF-26 头号场景）；
- 已声明但零订阅是**合法状态**，体现在 :attr:`DeliveryReport.subscriber_count`
  中，可观测而非不可见；
- ``declare_topic()`` 必须显式调用，禁止 ``subscribe`` 时隐式注册主题
  （那等于没做检查）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .fail_loud import FailLoudError, require_known, require_non_empty

__all__ = [
    "Subscriber",
    "SubscriberFailure",
    "DeliveryReport",
    "EventDeliveryError",
    "EventBus",
    "EVENT_BUS",
    "TOPIC_ALERT",
    "TOPIC_SYNC_PROGRESS",
    "TOPIC_QUARANTINE",
]

logger = logging.getLogger(__name__)

#: 订阅者签名：接收 ``(topic, event)``
Subscriber = Callable[[str, Any], None]

#: 约定主题
TOPIC_ALERT = "alert"
TOPIC_SYNC_PROGRESS = "sync.progress"
TOPIC_QUARANTINE = "data.quarantine"


@dataclass(frozen=True)
class SubscriberFailure:
    """一次订阅者投递失败的记录。

    Attributes:
        topic: 事件主题。
        subscriber: 订阅者的可读名称。
        error: 异常类型名 + 消息。
    """

    topic: str
    subscriber: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {"topic": self.topic, "subscriber": self.subscriber, "error": self.error}


@dataclass(frozen=True)
class DeliveryReport:
    """一次 :meth:`EventBus.publish` 的投递结果。

    Attributes:
        topic: 事件主题。
        delivered: 成功投递数。
        subscriber_count: 该主题的订阅者总数（投递前快照）。已声明但
            零订阅是合法状态，必须在此可观测（team-lead 裁决 1）。
        failures: 失败明细。
    """

    topic: str
    delivered: int
    subscriber_count: int = 0
    failures: list[SubscriberFailure] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """是否全部投递成功。"""
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "topic": self.topic,
            "delivered": self.delivered,
            "subscriber_count": self.subscriber_count,
            "failures": [f.to_dict() for f in self.failures],
        }


class EventDeliveryError(FailLoudError):
    """事件投递过程中至少有一个订阅者失败（``raise_on_error=True`` 时抛出）。

    Attributes:
        report: 完整的投递报告，便于上层写隔离区。
    """

    def __init__(self, report: DeliveryReport) -> None:
        detail = "; ".join(f"{f.subscriber}: {f.error}" for f in report.failures)
        super().__init__(
            f"[fail-loud/NF-26] 事件 {report.topic!r} 投递失败 "
            f"{len(report.failures)}/{report.delivered + len(report.failures)}：{detail}"
        )
        self.report = report


def _subscriber_name(fn: Subscriber) -> str:
    """取订阅者的可读名称（用于日志与失败记录）。"""
    module = getattr(fn, "__module__", "?")
    qualname = getattr(fn, "__qualname__", None) or repr(fn)
    return f"{module}.{qualname}"


class EventBus:
    """线程安全的进程内发布/订阅总线（含显式主题注册表）。

    Examples:
        >>> bus = EventBus()
        >>> received = []
        >>> _ = bus.subscribe("alert", lambda topic, event: received.append(event))
        >>> report = bus.publish("alert", {"code": "600000"})
        >>> report.delivered, received
        (1, [{'code': '600000'}])
        >>> report.subscriber_count
        1
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = {}
        #: 显式主题注册表（方案 C）：未声明的主题禁止 publish / subscribe
        self._known_topics: set[str] = set()
        self._lock = threading.RLock()
        # 内置约定主题在构造时统一声明（显式 declare_topic，不依赖隐式注册）
        self.declare_topic(TOPIC_ALERT)
        self.declare_topic(TOPIC_SYNC_PROGRESS)
        self.declare_topic(TOPIC_QUARANTINE)

    @staticmethod
    def _normalize(topic: str) -> str:
        """主题名归一化：去首尾空白，空主题显式报错。"""
        require_non_empty(topic, "事件主题")
        return str(topic).strip()

    def declare_topic(self, topic: str) -> None:
        """显式声明一个主题。

        未声明的主题禁止 ``publish`` / ``subscribe``（两侧都过
        :func:`require_known`），拼错主题名立刻报错而非静默落空。

        Args:
            topic: 主题名。

        Raises:
            UnknownValueError: 主题为空。
        """
        key = self._normalize(topic)
        with self._lock:
            self._known_topics.add(key)

    def subscribe(self, topic: str, callback: Subscriber) -> Callable[[], None]:
        """订阅一个主题。

        Args:
            topic: 主题名（必须已通过 :meth:`declare_topic` 声明）。
            callback: 形如 ``fn(topic, event)`` 的可调用对象。

        Returns:
            退订函数，调用即取消本次订阅。

        Raises:
            ValueError: ``callback`` 不可调用。
            UnknownValueError: 主题未声明（拼错主题名会在订阅侧立刻报错）。
        """
        if not callable(callback):
            raise ValueError(f"订阅者必须可调用，实际 {callback!r}")
        key = require_known(
            self._normalize(topic),
            "事件总线订阅主题",
            allowed=self._known_topics,
        )
        with self._lock:
            self._subscribers.setdefault(key, []).append(callback)

        def _unsubscribe() -> None:
            self.unsubscribe(key, callback)

        return _unsubscribe

    def unsubscribe(self, topic: str, callback: Subscriber) -> bool:
        """退订。

        Args:
            topic: 主题名。
            callback: 之前注册的可调用对象。

        Returns:
            是否确实移除了一个订阅。
        """
        key = self._normalize(topic)
        with self._lock:
            bucket = self._subscribers.get(key)
            if not bucket or callback not in bucket:
                return False
            bucket.remove(callback)
            if not bucket:
                del self._subscribers[key]
            return True

    def publish(self, topic: str, event: Any, *, raise_on_error: bool = False) -> DeliveryReport:
        """向主题的全部订阅者同步投递事件。

        Args:
            topic: 主题名（必须已通过 :meth:`declare_topic` 声明）。
            event: 事件载荷。
            raise_on_error: 有订阅者失败时是否抛 :class:`EventDeliveryError`。
                默认 ``False``：投递失败不应中断监控主循环，但**一定**会被
                记录进返回的报告与日志（不是静默吞掉）。

        Returns:
            :class:`DeliveryReport`（含 ``subscriber_count``，零订阅合法态可观测）。

        Raises:
            UnknownValueError: 主题未声明（告警发到拼错 topic 会立刻报错，
                而不是无声蒸发）。
            EventDeliveryError: ``raise_on_error=True`` 且存在失败订阅者。
        """
        key = require_known(
            self._normalize(topic),
            "事件总线发布主题",
            allowed=self._known_topics,
        )
        with self._lock:
            targets = list(self._subscribers.get(key, ()))
            subscriber_count = len(targets)

        delivered = 0
        failures: list[SubscriberFailure] = []
        for callback in targets:
            name = _subscriber_name(callback)
            try:
                callback(key, event)
            except Exception as exc:  # noqa: BLE001 - 隔离单个订阅者失败，但绝不静默
                logger.exception("事件订阅者投递失败: topic=%s subscriber=%s", key, name)
                failures.append(
                    SubscriberFailure(topic=key, subscriber=name, error=f"{type(exc).__name__}: {exc}")
                )
            else:
                delivered += 1

        report = DeliveryReport(
            topic=key, delivered=delivered, subscriber_count=subscriber_count, failures=failures
        )
        if failures and raise_on_error:
            raise EventDeliveryError(report)
        return report

    def subscriber_count(self, topic: str) -> int:
        """返回某主题的订阅者数量。"""
        with self._lock:
            return len(self._subscribers.get(str(topic).strip(), ()))

    def topics(self) -> list[str]:
        """返回当前有订阅者的主题列表（升序）。"""
        with self._lock:
            return sorted(self._subscribers)

    def clear(self) -> None:
        """清空全部订阅（主要供测试使用）。"""
        with self._lock:
            self._subscribers.clear()


#: 全局事件总线（进程内单例）
EVENT_BUS = EventBus()
