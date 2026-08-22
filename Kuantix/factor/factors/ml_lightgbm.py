"""基于梯度提升树（LightGBM / XGBoost，缺失时回退 sklearn）的 ML 因子。

因子含义：用历史窗口训练 GBDT 预测未来收益，输出「预测收益」作为信号。
依赖懒加载：lightgbm / xgboost / sklearn 任一可用即可；全缺则 ``compute`` 报错提示。

红线注意（R2）：业务层禁止 ``import easy_tdx``，所有上游符号经 ``factor_bridge``。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from Kuantix.adapters.factor_bridge import Factor, register_factor

from .ml_common import build_features, forward_return_label, walk_forward

__all__ = ["LightGBMReturnFactor"]


def _make_gbdt():
    """按可用依赖构造 GBDT 回归器工厂（优先 lightgbm → xgboost → sklearn）。"""
    try:
        import lightgbm as lgb  # noqa: F401

        def fit(X, y):
            model = lgb.LGBMRegressor(
                n_estimators=120, max_depth=4, learning_rate=0.05,
                num_leaves=15, min_child_samples=20, verbose=-1,
                n_jobs=1,
            )
            model.fit(X, y)
            return model

        return fit, "lightgbm"
    except Exception:  # noqa: BLE001
        pass
    try:
        import xgboost as xgb  # noqa: F401

        def fit(X, y):
            model = xgb.XGBRegressor(
                n_estimators=120, max_depth=4, learning_rate=0.05,
                n_jobs=1, verbosity=0,
            )
            model.fit(X, y)
            return model

        return fit, "xgboost"
    except Exception:  # noqa: BLE001
        pass
    from sklearn.ensemble import GradientBoostingRegressor

    def fit(X, y):
        model = GradientBoostingRegressor(
            n_estimators=120, max_depth=4, learning_rate=0.05
        )
        model.fit(X, y)
        return model

    return fit, "sklearn"


@register_factor
class LightGBMReturnFactor(Factor):
    """GBDT 预测收益因子：LightGBM/XGBoost/sklearn 梯度提升树预测未来收益。

    滚动前向训练（expanding window + 周期重训），输出预测收益作为信号。
    需要至少一种梯度提升库（lightgbm / xgboost / scikit-learn）。
    """

    name = "ml_gbdt_return"
    display_name = "LightGBM收益预测"
    category = "ml"
    description = (
        "梯度提升树（LightGBM/XGBoost/sklearn）预测未来收益的 ML 因子；"
        "滚动前向训练避免未来函数"
    )
    inputs = ("open", "high", "low", "close", "vol")

    def compute(self, df: pd.DataFrame) -> pd.Series:
        feats = build_features(df)
        label = forward_return_label(df, horizon=5)
        fit_fn, backend = _make_gbdt()
        preds = walk_forward(
            feats, label,
            min_samples=120, refit_every=60,
            fit_fn=fit_fn,
            predict_fn=lambda model, x: float(model.predict(x)[0]),
        )
        return preds
