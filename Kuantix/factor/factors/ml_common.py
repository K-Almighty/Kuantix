"""ML 因子公共工具（特征工程 / 标签 / 滚动前向训练）。

设计要点
--------
- 所有依赖（``lightgbm`` / ``xgboost`` / ``sklearn`` / ``torch``）均**懒加载**，
  缺失时给出清晰报错，不污染因子发现流程（R2/NF-1：业务层不直接 import 上游）。
- 因子输出为「预测的未来收益」信号（walk-forward，避免未来函数）：
  对第 ``i`` 根 K 线，用 ``[0, i)`` 区间训练，预测第 ``i`` 根对应的前瞻收益。
- 为控制复杂度，采用**周期性重训**（expanding window，每 ``refit_every`` 根重训一次），
  而非逐根重训。

红线注意（R2）：本文件仍属业务层，禁止 ``import easy_tdx``。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "build_features",
    "forward_return_label",
    "walk_forward",
]


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """从 OHLCV 构造模型特征矩阵（纯 pandas/numpy，无外部依赖）。

    Args:
        df: 含 ``open/high/low/close/vol`` 的 DataFrame（已按时间升序）。

    Returns:
        特征 DataFrame（索引对齐 ``df``，含 NaN 的行在训练时被丢弃）。
    """
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["vol"].astype(float).replace(0.0, np.nan)

    ret_1 = close.pct_change(1)
    ret_5 = close.pct_change(5)
    log_vol = np.log1p(vol.fillna(0.0))
    vol_ratio = (vol / vol.rolling(20).mean()).fillna(1.0)
    ma20 = close.rolling(20).mean()
    ma_gap = (close / ma20 - 1.0).fillna(0.0)
    hi20 = high.rolling(20).max()
    lo20 = low.rolling(20).min()
    span = (hi20 - lo20).replace(0.0, np.nan)
    close_position = ((close - lo20) / span).fillna(0.5)
    hl_spread = ((high - low) / close).fillna(0.0)

    feats = pd.DataFrame(
        {
            "ret_1": ret_1,
            "ret_5": ret_5,
            "log_vol": log_vol,
            "vol_ratio": vol_ratio,
            "ma_gap": ma_gap,
            "close_position": close_position,
            "hl_spread": hl_spread,
        },
        index=df.index,
    )
    return feats


def forward_return_label(df: pd.DataFrame, horizon: int = 5) -> pd.Series:
    """构造前瞻收益标签（避免未来函数：用 ``shift(-horizon)``）。

    Args:
        df: 含 ``close`` 的 DataFrame。
        horizon: 预测前瞻根数（默认 5 日）。

    Returns:
        前瞻收益序列（最后 ``horizon`` 根为 NaN）。
    """
    close = df["close"].astype(float)
    return (close.shift(-horizon) / close - 1.0)


def walk_forward(
    features: pd.DataFrame,
    labels: pd.Series,
    *,
    min_samples: int,
    refit_every: int,
    fit_fn,
    predict_fn,
) -> pd.Series:
    """滚动前向训练：expanding window + 周期重训，输出每根 K 线的预测值。

    Args:
        features: 特征矩阵（可能含 NaN）。
        labels: 前瞻收益标签（可能含 NaN）。
        min_samples: 开始训练所需最少样本。
        refit_every: 每隔多少根重训一次模型。
        fit_fn: ``(X: ndarray, y: ndarray) -> model``。
        predict_fn: ``(model, Xrow: ndarray) -> float``。

    Returns:
        与 ``features`` 同索引的预测序列（训练数据不足处为 NaN）。
    """
    n = len(features)
    out = np.full(n, np.nan, dtype=float)
    if n < min_samples:
        return pd.Series(out, index=features.index)

    data = pd.concat([features, labels.reindex(features.index)], axis=1)
    data.columns = list(features.columns) + ["__y"]
    data = data.dropna()
    if len(data) < min_samples:
        return pd.Series(out, index=features.index)

    feat_cols = list(features.columns)
    model = None
    last_fit_at = -10**9
    X_all = data[feat_cols].to_numpy(dtype=float)
    y_all = data["__y"].to_numpy(dtype=float)
    idx_all = list(data.index)
    # O4：索引查找从每次 O(n) 线性扫（get_indexer）降为 O(1) dict 查表。
    # 原实现对每个预测点调用 features.index.get_indexer([idx])，随样本数
    # 增长退化为 O(n²)；位置在 dropna 后与 features.index 的映射是固定的，
    # 预建一次即可。
    pos_to_feature_pos = {
        pos: int(features.index.get_indexer([idx_all[pos]])[0])
        for pos in range(len(data))
    }

    # 逐根预测（expanding：训练用 [0, i)）
    for pos in range(len(data)):
        if pos < min_samples:
            continue
        if model is None or (pos - last_fit_at) >= refit_every:
            try:
                X_train = X_all[:pos]
                y_train = y_all[:pos]
                model = fit_fn(X_train, y_train)
                last_fit_at = pos
            except Exception:  # noqa: BLE001 - 训练失败则该窗口跳过
                model = None
                continue
        try:
            x_row = X_all[pos : pos + 1]
            out[pos_to_feature_pos[pos]] = float(predict_fn(model, x_row))
        except Exception:  # noqa: BLE001 - 单点预测失败留 NaN
            continue
    return pd.Series(out, index=features.index)
