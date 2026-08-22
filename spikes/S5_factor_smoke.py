"""S5 — 关键组件冒烟（主链路真实跑通）。

真实执行：FactorEngine.compute_single 算因子 → compute_forward_returns 出前向收益
→ FactorAnalyzer.full_report 出 IC → RebalanceEngine.run 调仓回测
→ BacktestEngine.run 单标的回测。
用**合成行情数据**（网络无关）驱动真实代码路径，验证主链路真的能通。
"""
from __future__ import annotations

import time
import traceback

import numpy as np
import pandas as pd

from easy_tdx.backtest.engine import BacktestEngine
from easy_tdx.backtest.strategy import Strategy
from easy_tdx.factor.analysis import FactorAnalyzer
from easy_tdx.factor.engine import FactorEngine
from easy_tdx.portfolio.optimizer import EqualWeightOptimizer
from easy_tdx.portfolio.rebalance import RebalanceEngine

from common import save_result

SYMBOLS = ["600000", "600036", "000001", "000002", "002594"]
N = 300
BASE_COLS = ["open", "close", "high", "low", "vol", "amount", "datetime"]


def _make_data() -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(42)
    data = {}
    start = pd.Timestamp("2020-01-01")
    dates = pd.date_range(start, periods=N, freq="B")
    for code in SYMBOLS:
        price = 10 + np.cumsum(rng.normal(0, 0.02, N))
        price = np.maximum(price, 1.0)
        df = pd.DataFrame(
            {
                "open": price * (1 + rng.normal(0, 0.001, N)),
                "close": price,
                "high": price * (1 + abs(rng.normal(0, 0.005, N))),
                "low": price * (1 - abs(rng.normal(0, 0.005, N))),
                "vol": rng.integers(1e5, 1e6, N).astype(float),
                "amount": rng.integers(1e7, 1e8, N).astype(float),
            },
            index=dates,
        )
        df = df.copy()
        df["datetime"] = df.index
        data[code] = df
    return data


def _ma(arr, n):
    """self.I 会把 _SeriesAccessor 解包成 numpy 数组后传入，所以这里收 ndarray。"""
    return pd.Series(arr).rolling(n).mean().to_numpy()


class SMACross(Strategy):
    """正确写法（三个坑）：
    1. self.I(func, *args) 传 self.data.close（_SeriesAccessor），func 收到 ndarray；
    2. 指标在 next() 中必须用绝对下标 self.x[self._bar_index]，
       **不能**用 self.x[-1]（那是整条数组的最后一根，未来函数）；
    3. self.position 返回 {"size": float} —— dict 恒为真，
       判空必须写 self.position["size"] == 0。
    """

    def init(self):
        self.fast = self.I(_ma, self.data.close, 5)
        self.slow = self.I(_ma, self.data.close, 20)

    def next(self):
        i = self._bar_index
        f, s = self.fast[i], self.slow[i]
        if np.isnan(f) or np.isnan(s):
            return
        if f > s and self.position["size"] == 0:
            self.buy(size=0)
        elif f < s and self.position["size"] > 0:
            self.sell(size=0)


def _to_intdate(ts) -> int:
    return int(pd.Timestamp(ts).strftime("%Y%m%d"))


def main() -> dict:
    res: dict = {}
    print("=" * 80)
    print("S5 — 关键组件冒烟（合成数据驱动真实代码路径）")
    print("=" * 80)

    data = _make_data()
    fe = FactorEngine()

    # 1. FactorEngine.compute_single（增强每个 symbol 的 DataFrame）
    t0 = time.time()
    for code, df in data.items():
        data[code] = fe.compute_single(df, ["momentum_20d", "rsi_14"])
    has_factor = "momentum_20d" in next(iter(data.values())).columns
    factor_cols = [c for c in next(iter(data.values())).columns if c not in BASE_COLS]
    res["1_factor_engine"] = {"ok": has_factor, "seconds": round(time.time() - t0, 3), "factor_cols": factor_cols}
    print(f"[1] compute_single: ok={has_factor} factor_cols={factor_cols}")

    # 2. 前向收益（返回长表 date/code/forward_5d）
    t0 = time.time()
    fwd_df = fe.compute_forward_returns(data, period=5)
    fwd_col = "forward_5d"
    res["2_forward_returns"] = {"ok": not fwd_df.empty, "rows": len(fwd_df), "col": fwd_col,
                                "seconds": round(time.time() - t0, 3)}
    print(f"[2] compute_forward_returns: rows={len(fwd_df)}")

    # 3. 截面因子长表 → FactorAnalyzer
    #    注意坑：FactorAnalyzer.__init__ 内部会做 factor_data.merge(return_data[[date,code,ret]])，
    #    若 factor_data 已含 return_col，merge 会产生 _x/_y 后缀，导致后续 KeyError。
    #    所以 factor_data 必须**只含因子列**，不能预先 merge 前向收益。
    t0 = time.time()
    fdf = fe.compute_cross_section(data, ["momentum_20d"])
    retdf = fwd_df[["date", "code", fwd_col]]
    res["3a_cross_section"] = {"ok": not fdf.empty, "rows": len(fdf), "cols": list(fdf.columns)}
    print(f"[3a] compute_cross_section: rows={len(fdf)} cols={list(fdf.columns)}")
    try:
        fa = FactorAnalyzer(fdf, retdf, factor_col="momentum_20d", return_col=fwd_col)
        report = fa.full_report()
        cold = round(time.time() - t0, 3)
        t1 = time.time()
        FactorAnalyzer(fdf, retdf, factor_col="momentum_20d", return_col=fwd_col).full_report()
        warm = round(time.time() - t1, 3)
        res["3_factor_analyzer"] = {"ok": True, "ic_mean": getattr(report, "ic_mean", None),
                                    "ir": getattr(report, "ir", None),
                                    "seconds_cold": cold, "seconds_warm": warm}
        print(f"[3] FactorAnalyzer.full_report: IC_mean={report.ic_mean:.4f} IR={report.ir:.4f} "
              f"cold={cold}s warm={warm}s（冷启动含 scipy.stats 惰性导入）")
    except Exception as e:  # noqa: BLE001
        res["3_factor_analyzer"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(f"[3] FactorAnalyzer FAILED: {e}")

    # 4. RebalanceEngine.run（需要 dict 内每个 df 含 factor_name 列）
    t0 = time.time()
    try:
        rb = RebalanceEngine(optimizer=EqualWeightOptimizer(), factor_name="momentum_20d",
                             n_stocks=3, rebalance_freq="M", cash=1_000_000)
        all_dates = pd.concat([d["datetime"] for d in data.values()])
        start = _to_intdate(all_dates.min())
        end = _to_intdate(all_dates.max())
        rb_res = rb.run(data, start_date=start, end_date=end)
        res["4_rebalance"] = {"ok": True, "seconds": round(time.time() - t0, 3),
                              "rebalance_dates": len(getattr(rb_res, "rebalance_dates", []))}
        print(f"[4] RebalanceEngine.run: rebalance_dates={res['4_rebalance']['rebalance_dates']}")
    except Exception as e:  # noqa: BLE001
        res["4_rebalance"] = {"ok": False, "error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()[-400:]}
        print(f"[4] RebalanceEngine FAILED: {e}")

    # 5. BacktestEngine.run（单标的）
    t0 = time.time()
    try:
        bt = BacktestEngine(SMACross(), cash=100_000)
        single = data["600000"].copy()
        bt_res = bt.run(single)
        perf = getattr(bt_res, "performance", {})
        res["5_backtest"] = {"ok": True, "seconds": round(time.time() - t0, 3),
                             "total_return": perf.get("total_return"), "trades": len(getattr(bt_res, "trades", []))}
        print(f"[5] BacktestEngine.run: total_return={perf.get('total_return')}")
    except Exception as e:  # noqa: BLE001
        res["5_backtest"] = {"ok": False, "error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()[-400:]}
        print(f"[5] BacktestEngine FAILED: {e}")

    # 6. FactorAnalyzer 伸缩性实测（步骤3 耗时异常，必须量化）
    print("[6] FactorAnalyzer 伸缩性基准 …")
    scale = []
    for n_stocks, n_dates in [(5, 60), (5, 120), (20, 60), (50, 60), (1000, 60), (5400, 20)]:
        sub_codes = [f"{i:06d}" for i in range(n_stocks)]
        rng = np.random.default_rng(7)
        dts = [int(d.strftime("%Y%m%d")) for d in pd.date_range("2022-01-03", periods=n_dates, freq="B")]
        f_rows = {"date": np.repeat(dts, n_stocks), "code": sub_codes * n_dates,
                  "momentum_20d": rng.normal(0, 1, n_dates * n_stocks)}
        r_rows = {"date": np.repeat(dts, n_stocks), "code": sub_codes * n_dates,
                  fwd_col: rng.normal(0, 0.02, n_dates * n_stocks)}
        f_df, r_df = pd.DataFrame(f_rows), pd.DataFrame(r_rows)
        t0 = time.time()
        FactorAnalyzer(f_df, r_df, factor_col="momentum_20d", return_col=fwd_col).full_report()
        el = round(time.time() - t0, 3)
        scale.append({"stocks": n_stocks, "dates": n_dates, "rows": n_dates * n_stocks, "seconds": el})
        print(f"     stocks={n_stocks:3d} dates={n_dates:3d} rows={n_dates*n_stocks:6d} -> {el}s")
    res["6_analyzer_scaling"] = scale

    res["status"] = "pass" if all(
        res.get(k, {}).get("ok") for k in
        ["1_factor_engine", "2_forward_returns", "3_factor_analyzer", "4_rebalance", "5_backtest"]
    ) else "partial"
    save_result("S5_factor_smoke", res)
    return res


if __name__ == "__main__":
    main()
