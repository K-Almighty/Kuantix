"""因子中文名映射（NF：统一中文展示）。

因子 ``name`` 是内部英文标识符（同时作为存储键 / API 查询键，不能改），
但 UI 与报告需要中文展示名。内置因子可在各自类上直接定义 ``display_name``；
本表作为**兜底**，覆盖上游 ``easy_tdx`` 内置因子（无法改其源码）以及尚未
显式定义 ``display_name`` 的因子。

优先级（见 :func:`Kuantix.api.routers.factor._factor_info`）：
``因子类.display_name`` > ``FACTOR_DISPLAY_NAMES[name]`` > ``None``。
"""
from __future__ import annotations

#: 因子英文名 -> 中文展示名
FACTOR_DISPLAY_NAMES: dict[str, str] = {
    # 上游 easy_tdx 内置因子
    "amount_ma_ratio": "成交额MA比",
    "atr_14d": "ATR(14日)",
    "boll_position": "布林带位置",
    "chanlun_bi_dir": "缠论笔方向",
    "chanlun_mmd": "缠论买卖点",
    "macd_hist_signal": "MACD柱信号",
    "max_drawdown_20d": "20日最大回撤",
    "momentum_20d": "20日动量",
    "momentum_60d": "60日动量",
    "obv_trend": "OBV趋势",
    "pb_ratio": "市净率",
    "pe_ratio": "市盈率",
    "reversal_5d": "5日反转",
    "rsi_14": "RSI(14)",
    "sharpe_20d": "20日夏普比率",
    "turnover_rate": "换手率",
    "vol_surge": "量比异动",
    "volatility_20d": "20日波动率",
    "win_rate_20d": "20日胜率",
    # 本项目内置因子（类上已定义 display_name，这里仅作冗余兜底）
    "close_position_20d": "20日收盘位置",
    "volume_ratio_5d": "5日量比",
    "ml_gbdt_return": "LightGBM收益预测",
    "ml_gp_signal": "遗传规划信号",
    "ml_lstm_return": "LSTM收益预测",
    "ml_transformer_return": "Transformer收益预测",
}


def display_name_of(name: str, fallback: str | None = None) -> str | None:
    """返回因子 ``name`` 的中文展示名；未登记时返回 ``fallback``。"""
    return FACTOR_DISPLAY_NAMES.get(name, fallback)
