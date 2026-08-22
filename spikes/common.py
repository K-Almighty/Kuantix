"""Phase 1.5 Spike 共享工具。

硬性约束：
- 只读 ~/.easy_tdx/config.json（绝不写入）。
- 绝不调用 from_best_host / save_best_host（避免写 ~/.easy_tdx）。
- 所有客户端显式传 host/port。
- 不修改 easy_tdx-main 任何文件（仅 import）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

Kuantix_ROOT = Path("/Users/kongbiao/Downloads/开源量化/Kuantix")
SPIKE_ROOT = Kuantix_ROOT / "spikes"
RESULTS_DIR = SPIKE_ROOT / "results"
VIPDOC_ROOT = Path.home() / ".Kuantix" / "vipdoc"
EASY_TDX_CONFIG = Path.home() / ".easy_tdx" / "config.json"

RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def load_tdx_config() -> dict:
    """只读加载用户已测速节点配置。"""
    if not EASY_TDX_CONFIG.exists():
        return {}
    with open(EASY_TDX_CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)


CFG = load_tdx_config()
BEST_HOST = CFG.get("best_host", "180.153.18.170")
BEST_MAC_HOST = CFG.get("best_mac_host", "123.60.47.136")
PORT = int(CFG.get("port", 7709))
KNOWN_HOSTS = CFG.get("known_hosts", [])
MAC_HOSTS = CFG.get("mac_hosts", [])
# 扩展市场（港股/美股/期货）：MAC EX 协议走 7727，需 Login 握手
MAC_EX_HOSTS = CFG.get("mac_ex_hosts", ["116.205.135.205", "121.37.232.167"])
EX_HOSTS = CFG.get("ex_hosts", [])
EX_PORT = 7727


def save_result(name: str, payload: dict) -> None:
    out = RESULTS_DIR / f"{name}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def df_to_security_bars(df: pd.DataFrame, vol_divisor: float = 100.0):
    """把 MacClient.get_stock_kline 返回的 DataFrame 转成 list[SecurityBar]。

    RD-8（实测）：MacClient 的 ``vol`` 单位是**股**，而 vipdoc .day 的写入约定是
    ``stored_uint32 = vol / vol_coeff``（A 股 vol_coeff=0.01），读回再 ``× 0.01``。
    也就是说 **写入时 bar.vol 必须是「手」**，否则：
      - 股 / 0.01 = 股 × 100 → 轻易超过 uint32 上限 4294967295，struct.error；
      - 即便不溢出，读回也会与在线口径差 100 倍。
    因此这里默认 ``vol_divisor=100``（股 → 手）。
    """
    from easy_tdx.models.bar import SecurityBar

    # 日期来源：DatetimeIndex 或 datetime 列
    if isinstance(df.index, pd.DatetimeIndex):
        dates = df.index
    elif "datetime" in df.columns:
        dates = pd.to_datetime(df["datetime"])
    elif "date" in df.columns:
        dates = pd.to_datetime(df["date"])
    else:
        raise ValueError(f"找不到日期列，columns={list(df.columns)}")

    bars = []
    for i, (idx, row) in enumerate(df.iterrows()):
        dt = pd.Timestamp(dates[i])
        bars.append(
            SecurityBar(
                open=float(row["open"]),
                close=float(row["close"]),
                high=float(row["high"]),
                low=float(row["low"]),
                vol=float(row["vol"]) / vol_divisor,
                amount=float(row["amount"]),
                year=int(dt.year),
                month=int(dt.month),
                day=int(dt.day),
                hour=0,
                minute=0,
            )
        )
    return bars


def df_to_ex_daily_bars(df: pd.DataFrame):
    """把扩展市场 DataFrame 转成 list[ExDailyBar]（settlement 兜底为 close）。"""
    from easy_tdx.offline.ex_daily_bar import ExDailyBar

    if isinstance(df.index, pd.DatetimeIndex):
        dates = df.index
    elif "datetime" in df.columns:
        dates = pd.to_datetime(df["datetime"])
    else:
        dates = pd.to_datetime(df["date"])

    bars = []
    for i, (_, row) in enumerate(df.iterrows()):
        dt = pd.Timestamp(dates[i])
        bars.append(
            ExDailyBar(
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                amount=0,
                vol=int(row["vol"]) if "vol" in row else 0,
                settlement=float(row["close"]),
                hk_stock_amount=0.0,
                year=int(dt.year),
                month=int(dt.month),
                day=int(dt.day),
            )
        )
    return bars
