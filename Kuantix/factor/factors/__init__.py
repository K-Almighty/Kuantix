"""自定义因子自动发现（注册到上游 ``FACTORY_REGISTRY``）。

设计：
- 内置两个示例因子（:class:`VolumeRatioFactor` / :class:`ClosePositionFactor`），
  演示如何继承上游 :class:`~easy_tdx.factor.base.Factor` 并注册；
- :func:`discover_factors` 扫描 ``Kuantix/factor/factors/`` 目录下的
  ``*.py``（排除 ``__init__.py`` 本身），import 触发 ``@register_factor``，
  返回当前注册表里新增的因子名 —— 这就是「自定义因子放 ``factors/`` 后
  ``Kuantix factor list`` 能发现」的机制。

红线注意（NF-1/R2）：本目录是**业务层**，禁止直接 ``import easy_tdx``；
一切上游符号（``Factor`` / ``register_factor`` / ``FACTORY_REGISTRY``）都从
:mod:`Kuantix.adapters.factor_bridge` 间接获取。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pandas as pd

from Kuantix.adapters.factor_bridge import (
    FACTORY_REGISTRY,
    Factor,
    register_factor,
)

__all__ = [
    "FACTORY_REGISTRY",
    "Factor",
    "register_factor",
    "VolumeRatioFactor",
    "ClosePositionFactor",
    "discover_factors",
]

#: 本目录（自动发现默认扫描路径）
FACTORS_DIR = Path(__file__).resolve().parent


@register_factor
class VolumeRatioFactor(Factor):
    """5 日量比因子：近 5 日平均成交量 / 近 20 日平均成交量。"""

    name = "volume_ratio_5d"
    display_name = "5日量比"
    category = "volume"
    description = "5 日量比：短期量能相对中期基准的放大倍数（>1 放量，<1 缩量）"
    inputs = ("vol",)

    def compute(self, df: pd.DataFrame) -> pd.Series:
        vol = df["vol"].astype(float)
        short = vol.rolling(5).mean()
        base = vol.rolling(20).mean()
        ratio = short / base
        return ratio.fillna(1.0)


@register_factor
class ClosePositionFactor(Factor):
    """20 日收盘位置因子：收盘价在近 20 日高低区间内的百分位（0~1）。"""

    name = "close_position_20d"
    display_name = "20日收盘位置"
    category = "technical"
    description = "收盘位置：0=区间最低，1=区间最高（趋势强度代理）"
    inputs = ("close", "high", "low")

    def compute(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"].astype(float)
        high = df["high"].astype(float).rolling(20).max()
        low = df["low"].astype(float).rolling(20).min()
        span = (high - low).replace(0.0, pd.NA)
        position = (close - low) / span
        return position.fillna(0.5)


def discover_factors(directory: Path | str | None = None) -> list[str]:
    """扫描因子目录，import 所有模块触发注册，返回本次新增的因子名。

    Args:
        directory: 扫描目录；``None`` 使用本文件所在目录。

    Returns:
        新增因子名列表（升序）。
    """
    target = Path(directory).expanduser() if directory is not None else FACTORS_DIR
    if not target.is_dir():
        return []
    before = set(FACTORY_REGISTRY)
    for path in sorted(target.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module_name = f"Kuantix.factor.factors.{path.stem}"
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - 单个因子失败不拖垮发现流程
            # 记录到该模块的 _load_error 便于诊断（fail-loud：不静默）
            setattr(
                FACTORS_DIR.joinpath(path.name),
                "_load_error",
                f"{type(exc).__name__}: {exc}",
            )
            continue
    return sorted(set(FACTORY_REGISTRY) - before)
