"""S1 — API 签名运行时校验。

对报告 ②A-E 中标★的组件，import 后用 inspect.signature 打印真实签名，
与报告逐条比对。重点组件：MacClient.get_stock_kline / FactorEngine.compute_cross_section
/ FactorAnalyzer.full_report / RebalanceEngine.run / BacktestEngine.run /
SignalScanner.scan / offline.write_daily.* 。
"""
from __future__ import annotations

import importlib
import inspect

from common import save_result

TARGETS = [
    ("MacClient.get_stock_kline", "easy_tdx.mac.client", "MacClient", "get_stock_kline"),
    ("FactorEngine.compute_cross_section", "easy_tdx.factor.engine", "FactorEngine", "compute_cross_section"),
    ("FactorAnalyzer.full_report", "easy_tdx.factor.analysis", "FactorAnalyzer", "full_report"),
    ("RebalanceEngine.run", "easy_tdx.portfolio.rebalance", "RebalanceEngine", "run"),
    ("BacktestEngine.run", "easy_tdx.backtest.engine", "BacktestEngine", "run"),
    ("SignalScanner.scan", "easy_tdx.screen.scanner", "SignalScanner", "scan"),
    ("FactorEngine.compute_forward_returns", "easy_tdx.factor.engine", "FactorEngine", "compute_forward_returns"),
    ("StrengthRanker.rank", "easy_tdx.screen.strength", "StrengthRanker", "rank"),
    ("ChanlunAnalyser.process_klines", "easy_tdx.chanlun.analyser", "ChanlunAnalyser", "process_klines"),
    ("sync_daily_bars_from_security_bars", "easy_tdx.offline.write_daily", None, "sync_daily_bars_from_security_bars"),
    ("append_daily_bars", "easy_tdx.offline.write_daily", None, "append_daily_bars"),
    ("get_last_bar_date", "easy_tdx.offline.write_daily", None, "get_last_bar_date"),
    ("read_daily_bars", "easy_tdx.offline.daily_bar", None, "read_daily_bars"),
    ("sync_ex_daily_bars", "easy_tdx.offline.write_ex_daily", None, "sync_ex_daily_bars"),
    ("append_5min_bars", "easy_tdx.offline.write_min_bar", None, "append_5min_bars"),
]


def main() -> dict:
    results = {}
    print("=" * 80)
    print("S1 — API 签名运行时校验")
    print("=" * 80)
    for label, mod_name, cls_name, fn_name in TARGETS:
        try:
            mod = importlib.import_module(mod_name)
            obj = getattr(mod, cls_name) if cls_name else mod
            fn = getattr(obj, fn_name)
            sig = str(inspect.signature(fn))
            results[label] = {"status": "ok", "signature": f"{label}{sig}"}
            print(f"[OK]   {label}{sig}")
        except Exception as e:  # noqa: BLE001
            results[label] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            print(f"[FAIL] {label} -> {type(e).__name__}: {e}")
    save_result("S1_signature", results)
    ok = sum(1 for v in results.values() if v["status"] == "ok")
    print(f"\nS1 汇总：{ok}/{len(TARGETS)} 组件签名成功获取")
    return results


if __name__ == "__main__":
    main()
