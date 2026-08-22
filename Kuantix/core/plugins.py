"""插件注册表（NF-2）。

六类扩展点统一走一张注册表：因子、预警判据、推送通道、选股过滤器、
券商 CSV 模板、市场档案。

fail-loud 要点（NF-26）：
- 解析不存在的插件名 → 抛 :class:`~Kuantix.core.fail_loud.MissingKeyError`，
  绝不回落到"默认插件"；
- 自动发现目录时，任何模块 import 失败 → 抛
  :class:`PluginLoadError`，绝不 ``try/except: pass`` 跳过；
- 重复注册同名插件 → 抛 :class:`PluginConflictError`，避免"后者静默覆盖前者"。
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Iterator
from enum import Enum
from types import ModuleType
from typing import Any, TypeVar

from .fail_loud import FailLoudError, require_key, require_known

__all__ = [
    "PluginKind",
    "PluginLoadError",
    "PluginConflictError",
    "PluginRegistry",
    "REGISTRY",
    "register_plugin",
    "discover_plugins",
]

T = TypeVar("T")


class PluginKind(str, Enum):
    """插件类别（六类扩展点，NF-2）。"""

    FACTOR = "factor"  # 自定义因子
    CRITERION = "criterion"  # 预警判据
    NOTIFY_CHANNEL = "notify_channel"  # 推送通道
    SCREEN_FILTER = "screen_filter"  # 选股过滤器
    BROKER_TEMPLATE = "broker_template"  # 券商 CSV 模板
    MARKET_PROFILE = "market_profile"  # 市场档案


class PluginLoadError(FailLoudError):
    """插件模块加载失败（NF-26：显式报错，不静默跳过）。"""


class PluginConflictError(FailLoudError):
    """同类别下插件重名（拒绝静默覆盖）。"""


class PluginRegistry:
    """插件注册表。

    结构为 ``{kind: {name: cls}}``，同类别内按名称唯一。
    """

    def __init__(self) -> None:
        self._store: dict[PluginKind, dict[str, type]] = {kind: {} for kind in PluginKind}

    # -- 写入 -------------------------------------------------------------

    def register(self, kind: PluginKind | str, name: str, cls: type) -> type:
        """注册一个插件类。

        Args:
            kind: 插件类别。
            name: 插件名（同类别内唯一，大小写不敏感，统一转小写存储）。
            cls: 插件类。

        Returns:
            原样返回 ``cls``（便于用作装饰器）。

        Raises:
            UnknownValueError: ``kind`` 不是合法类别。
            ValueError: ``name`` 为空。
            PluginConflictError: 同类别下已存在同名插件。
        """
        plugin_kind = self._coerce_kind(kind)
        key = str(name).strip().lower()
        if not key:
            raise ValueError(f"插件名不能为空（kind={plugin_kind.value}, cls={cls!r}）")
        bucket = self._store[plugin_kind]
        if key in bucket and bucket[key] is not cls:
            raise PluginConflictError(
                f"[fail-loud/NF-26] 插件重名：kind={plugin_kind.value} name={key!r} "
                f"已被 {bucket[key]!r} 占用，拒绝被 {cls!r} 静默覆盖"
            )
        bucket[key] = cls
        return cls

    def unregister(self, kind: PluginKind | str, name: str) -> None:
        """注销插件（主要供测试使用）。

        Args:
            kind: 插件类别。
            name: 插件名。

        Raises:
            MissingKeyError: 插件不存在。
        """
        plugin_kind = self._coerce_kind(kind)
        key = str(name).strip().lower()
        bucket = self._store[plugin_kind]
        require_key(bucket, key, f"注销插件 kind={plugin_kind.value}")
        del bucket[key]

    # -- 读取 -------------------------------------------------------------

    def resolve(self, kind: PluginKind | str, name: str) -> type:
        """按类别+名称解析插件类。

        Args:
            kind: 插件类别。
            name: 插件名。

        Returns:
            插件类。

        Raises:
            MissingKeyError: 插件未注册（**不回落到默认插件**）。
        """
        plugin_kind = self._coerce_kind(kind)
        key = str(name).strip().lower()
        return require_key(
            self._store[plugin_kind],
            key,
            f"解析插件 kind={plugin_kind.value}",
        )

    def all(self, kind: PluginKind | str) -> dict[str, type]:
        """返回某类别下全部插件的浅拷贝 ``{name: cls}``。"""
        plugin_kind = self._coerce_kind(kind)
        return dict(self._store[plugin_kind])

    def names(self, kind: PluginKind | str) -> list[str]:
        """返回某类别下全部插件名（升序）。"""
        return sorted(self.all(kind))

    def kinds(self) -> list[PluginKind]:
        """返回全部插件类别。"""
        return list(PluginKind)

    def snapshot(self) -> dict[str, list[str]]:
        """返回 ``{类别: [插件名…]}`` 的全量快照（供 CLI/REST 展示）。"""
        return {kind.value: self.names(kind) for kind in PluginKind}

    def __iter__(self) -> Iterator[tuple[PluginKind, str, type]]:
        """遍历 ``(kind, name, cls)`` 三元组。"""
        for kind, bucket in self._store.items():
            for name, cls in sorted(bucket.items()):
                yield kind, name, cls

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self._store.values())

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _coerce_kind(kind: PluginKind | str) -> PluginKind:
        """把字符串/枚举统一为 :class:`PluginKind`，非法值 fail-loud。"""
        if isinstance(kind, PluginKind):
            return kind
        value = require_known(
            str(kind).strip().lower(),
            "插件类别解析",
            allowed={k.value for k in PluginKind},
        )
        return PluginKind(value)


#: 全局插件注册表（进程内单例）
REGISTRY = PluginRegistry()


def register_plugin(
    kind: PluginKind | str,
    name: str,
    registry: PluginRegistry | None = None,
) -> Callable[[type], type]:
    """类装饰器：把类注册到插件表。

    Args:
        kind: 插件类别。
        name: 插件名。
        registry: 目标注册表；``None`` 使用全局 :data:`REGISTRY`。

    Returns:
        装饰器函数。

    Examples:
        >>> from Kuantix.core.plugins import PluginKind, PluginRegistry, register_plugin
        >>> reg = PluginRegistry()
        >>> @register_plugin(PluginKind.NOTIFY_CHANNEL, "dummy", registry=reg)
        ... class DummyChannel:
        ...     pass
        >>> reg.resolve(PluginKind.NOTIFY_CHANNEL, "dummy") is DummyChannel
        True
    """
    target = registry if registry is not None else REGISTRY

    def decorator(cls: type) -> type:
        target.register(kind, name, cls)
        return cls

    return decorator


def discover_plugins(package: str | ModuleType) -> list[str]:
    """递归 import 一个包下的全部子模块，触发其中的 ``@register_plugin``。

    用于「把自定义因子丢进 ``factor/factors/`` 目录即自动生效」这类场景。

    Args:
        package: 包名或已导入的包对象。

    Returns:
        成功导入的模块全名列表（升序）。

    Raises:
        PluginLoadError: 包本身或任一子模块 import 失败。
            **刻意不跳过**：一个坏插件必须暴露出来，而不是让用户以为
            自己的因子已经生效（NF-26）。
    """
    if isinstance(package, str):
        try:
            pkg: ModuleType = importlib.import_module(package)
        except Exception as exc:  # noqa: BLE001 - 统一转成 fail-loud 异常
            raise PluginLoadError(
                f"[fail-loud/NF-26] 插件包 {package!r} 导入失败: {type(exc).__name__}: {exc}"
            ) from exc
    else:
        pkg = package

    search_paths: Any = getattr(pkg, "__path__", None)
    if search_paths is None:
        raise PluginLoadError(
            f"[fail-loud/NF-26] {pkg.__name__!r} 不是包（无 __path__），无法做插件发现"
        )

    loaded: list[str] = []
    for module_info in pkgutil.walk_packages(search_paths, prefix=f"{pkg.__name__}."):
        try:
            importlib.import_module(module_info.name)
        except Exception as exc:  # noqa: BLE001 - 统一转成 fail-loud 异常
            raise PluginLoadError(
                f"[fail-loud/NF-26] 插件模块 {module_info.name!r} 导入失败: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        loaded.append(module_info.name)
    return sorted(loaded)
