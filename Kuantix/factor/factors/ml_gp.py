"""遗传规划（Genetic Programming）符号因子挖掘。

不依赖任何 ML 框架，用标准库 + numpy 演化「特征组合公式」，最大化与前瞻收益的
排序相关性（IC），输出公式取值作为因子信号。滚动前向：周期性在扩展窗口上重演化。

红线注意（R2）：业务层禁止 ``import easy_tdx``。
"""

from __future__ import annotations

import random
from typing import Any

import numpy as np
import pandas as pd

from Kuantix.adapters.factor_bridge import Factor, register_factor

from .ml_common import build_features, forward_return_label

__all__ = ["GeneticProgrammingFactor"]

#: 二元 / 一元原语（div 受保护，避免除零）
_PRIM2 = {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
    "div": lambda a, b: np.where(np.abs(b) < 1e-9, 0.0, a / np.where(np.abs(b) < 1e-9, 1.0, b)),
}
_PRIM1 = {"neg": lambda a: -a}
_CONSTANTS = [-2.0, -1.0, -0.5, 0.5, 1.0, 2.0]


def _eval(node: Any, env: dict) -> np.ndarray:
    """递归求值公式树。``node`` 形如 ``('add', a, b)`` / ``('var', name)`` / ``('const', v)``。"""
    if isinstance(node, tuple):
        op = node[0]
        if op in _PRIM2:
            return _PRIM2[op](_eval(node[1], env), _eval(node[2], env))
        if op in _PRIM1:
            return _PRIM1[op](_eval(node[1], env))
        raise ValueError(f"未知算子 {op}")
    if isinstance(node, str):  # 变量名
        return env[node]
    return np.full(len(next(iter(env.values()))), float(node), dtype=float)  # const


def _random_tree(features: list[str], depth: int, rng: random.Random) -> Any:
    if depth <= 0 or (depth < 3 and rng.random() < 0.3):
        if rng.random() < 0.5:
            return rng.choice(features)
        return rng.choice(_CONSTANTS)
    r = rng.random()
    if r < 0.4:
        return ("neg", _random_tree(features, depth - 1, rng))
    op = rng.choice(list(_PRIM2))
    return (op, _random_tree(features, depth - 1, rng), _random_tree(features, depth - 1, rng))


def _mutate(node: Any, features: list[str], depth: int, rng: random.Random) -> Any:
    if rng.random() < 0.25:
        return _random_tree(features, depth, rng)
    if isinstance(node, tuple):
        return (node[0], *(_mutate(c, features, depth - 1, rng) for c in node[1:]))
    return node


def _crossover(a: Any, b: Any, rng: random.Random) -> Any:
    if rng.random() < 0.5 and isinstance(b, tuple) and len(b) > 1:
        return b
    if isinstance(a, tuple) and len(a) > 1:
        return (a[0], *(_crossover(c, b, rng) for c in a[1:]))
    return a


def _ic(tree: Any, env: dict, y: np.ndarray) -> float:
    """公式树与标签的排序相关性（IC）。"""
    try:
        pred = _eval(tree, env)
    except Exception:  # noqa: BLE001
        return -1.0
    if not np.all(np.isfinite(pred)):
        return -1.0
    pr = pd.Series(pred).rank().to_numpy()
    yr = pd.Series(y).rank().to_numpy()
    if pr.std() < 1e-9 or yr.std() < 1e-9:
        return -1.0
    return float(np.corrcoef(pr, yr)[0, 1])


def _evolve(feat_df: pd.DataFrame, y: np.ndarray, *, pop: int, gens: int, seed: int) -> Any:
    """在给定窗口上演化最优公式树。"""
    rng = random.Random(seed)
    feats = list(feat_df.columns)
    env = {c: feat_df[c].to_numpy(dtype=float) for c in feats}
    pop_trees = [_random_tree(feats, 3, rng) for _ in range(pop)]

    def fitness(t):
        return _ic(t, env, y)

    scored = sorted(pop_trees, key=fitness, reverse=True)
    for _ in range(gens):
        new = scored[: max(2, pop // 10)]  # 精英保留
        while len(new) < pop:
            p1, p2 = rng.sample(scored[: max(4, pop // 3)], 2)
            child = _mutate(_crossover(p1, p2, rng), feats, 3, rng)
            new.append(child)
        scored = sorted(new, key=fitness, reverse=True)
    return scored[0]


@register_factor
class GeneticProgrammingFactor(Factor):
    """遗传规划因子：自动演化特征组合公式，最大化与未来收益的排序相关（IC）。

    纯标准库 + numpy 实现，无需任何 ML 框架。滚动前向：周期性在扩展窗口重演化。
    """

    name = "ml_gp_signal"
    display_name = "遗传规划信号"
    category = "ml"
    description = (
        "遗传规划符号回归因子：演化特征组合公式预测未来收益；"
        "滚动前向训练避免未来函数"
    )
    inputs = ("open", "high", "low", "close", "vol")

    def compute(self, df: pd.DataFrame) -> pd.Series:
        feats = build_features(df)
        label = forward_return_label(df, horizon=5)
        data = pd.concat([feats, label.reindex(feats.index).rename("__y")], axis=1).dropna()
        n = len(data)
        out = np.full(len(feats), np.nan, dtype=float)
        if n < 120:
            return pd.Series(out, index=feats.index)

        feat_cols = list(feats.columns)
        tree = None
        last_fit = -10**9
        X = data[feat_cols]
        y = data["__y"].to_numpy(dtype=float)
        idx = list(data.index)

        for pos in range(len(data)):
            if pos < 120:
                continue
            if tree is None or (pos - last_fit) >= 120:
                window = slice(max(0, pos - 400), pos)
                try:
                    tree = _evolve(X.iloc[window], y[window], pop=30, gens=6, seed=pos)
                except Exception:  # noqa: BLE001
                    tree = None
                last_fit = pos
            if tree is None:
                continue
            env = {c: X.iloc[pos : pos + 1][c].to_numpy(dtype=float) for c in feat_cols}
            try:
                val = float(_eval(tree, env)[0])
                out[feats.index.get_indexer([idx[pos]])[0]] = val if np.isfinite(val) else np.nan
            except Exception:  # noqa: BLE001
                continue
        return pd.Series(out, index=feats.index)
