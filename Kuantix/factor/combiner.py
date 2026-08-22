"""因子合成（等权 / IC 加权 / IR 加权）。

:class:`FactorCombiner.combine` 把多个因子的截面值合成一个综合分：

- **equal** —— 各因子 z-score 后等权平均；
- **ic** —— 各因子 z-score 后按 IC 绝对值加权（权重归一化）；
- **ir** —— 各因子 z-score 后按 IR 加权（权重归一化）。

fail-loud（NF-26）：方法名必须是已知集合；缺少权重的因子显式报错，
不用 0 静默填充。
"""

from __future__ import annotations

from typing import Mapping

import pandas as pd

from Kuantix.core.fail_loud import require_known, require_key

__all__ = ["FactorCombiner", "COMBINE_METHODS"]

COMBINE_METHODS: tuple[str, ...] = ("equal", "ic", "ir")


class FactorCombiner:
    """多因子合成器。

    Examples:
        >>> combiner = FactorCombiner()
        >>> values = pd.DataFrame({"f1": [1.0, 2.0], "f2": [3.0, 1.0]},
        ...                       index=["000001", "000002"])
        >>> series = combiner.combine(values, "equal")
        >>> len(series)
        2
    """

    def combine(
        self,
        values: pd.DataFrame,
        method: str,
        weights: Mapping[str, float] | None = None,
    ) -> pd.Series:
        """合成综合因子分。

        Args:
            values: 因子截面表，index=code，columns=因子名。
            method: 合成方法（``equal`` / ``ic`` / ``ir``）。
            weights: 权重映射 ``{因子名: 权重}``；``equal`` 可省略，
                ``ic`` / ``ir`` 必须提供（缺因子即报错）。

        Returns:
            index=code 的综合分 Series（已排序降序）。

        Raises:
            Kuantix.core.fail_loud.UnknownValueError: 方法名未知。
            Kuantix.core.fail_loud.MissingKeyError: ic/ir 缺少某因子权重。
        """
        require_known(method, "因子合成方法", allowed=set(COMBINE_METHODS))
        if values.empty:
            return pd.Series(dtype=float)

        factors = list(values.columns)
        if not factors:
            raise ValueError("[fail-loud/NF-26] 合成输入为空（无因子列）")

        z = values.apply(self._zscore, axis=0)
        if method == "equal":
            n = float(len(factors))
            score = z.sum(axis=1) / n
        else:
            effective_weights: dict[str, float] = {}
            for factor in factors:
                w = require_key(weights or {}, factor, f"因子合成权重({method})")
                effective_weights[factor] = abs(float(w))
            total = sum(effective_weights.values())
            if total <= 0:
                raise ValueError(
                    f"[fail-loud/NF-26] {method} 权重之和为 0，无法归一化"
                )
            score = pd.Series(0.0, index=values.index)
            for factor in factors:
                score += z[factor] * (effective_weights[factor] / total)
        return score.sort_values(ascending=False)

    @staticmethod
    def _zscore(series: pd.Series) -> pd.Series:
        """z-score 标准化；标准差为 0 时返回 0（该因子无区分度）。"""
        std = series.std(ddof=0)
        if std is None or std == 0 or pd.isna(std):
            return pd.Series(0.0, index=series.index)
        mean = series.mean()
        return (series - mean) / std
