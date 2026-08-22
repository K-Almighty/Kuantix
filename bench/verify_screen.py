"""screen 链路优化后的正确性 + 耗时快检。

对比三条用例的结果指纹与 baseline_before.json 是否完全一致：
    .venv/bin/python bench/verify_screen.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR.parent))
sys.path.insert(0, str(BENCH_DIR))

from bench_Kuantix import sig_screen  # noqa: E402

from Kuantix.adapters.factor_bridge import L1Reader  # noqa: E402
from Kuantix.config import get_config  # noqa: E402
from Kuantix.data.market_store import MarketStore  # noqa: E402
from Kuantix.factor.store import FactorStore  # noqa: E402
from Kuantix.screen.service import ScreenRequest, ScreenService  # noqa: E402


def main() -> int:
    baseline = {
        r["name"]: r
        for r in json.loads((BENCH_DIR / "baseline_before.json").read_text())["results"]
    }

    config = get_config()
    store = MarketStore()
    reader = L1Reader(config.paths.vipdoc, backend="auto", store=store)
    factor_store = FactorStore(config.paths.factors, config.paths.db)
    screen = ScreenService(config, store=factor_store, reader=reader)

    factors = [p.name for p in sorted(Path(config.paths.factors).iterdir()) if p.is_dir()]
    big_factor = "momentum_60d" if "momentum_60d" in factors else factors[0]
    multi = factors[:5]

    cases = [
        (
            "screen.factor_top50_nocond",
            lambda: screen.screen_factor(
                factor=big_factor, market="CN", top_n=50, order="desc",
                as_of=None, tech_cond={}, chanlun_cond={},
            ),
        ),
        (
            "screen.factor_top50_techcond",
            lambda: screen.screen_factor(
                factor=big_factor, market="CN", top_n=50, order="desc",
                as_of=None, tech_cond={"min_close": 5.0}, chanlun_cond={},
            ),
        ),
        (
            "screen.multifactor_top50",
            lambda: screen.run(ScreenRequest(market="CN", factors=tuple(multi), top_n=50)),
        ),
    ]

    failures = 0
    for name, fn in cases:
        t0 = time.perf_counter()
        raw = fn()
        elapsed = (time.perf_counter() - t0) * 1000
        payload = raw[0] if isinstance(raw, tuple) else raw
        sig = sig_screen(payload)
        want = baseline[name]["result_signature"]
        before_ms = baseline[name]["wall_median_ms"]
        ok = sig == want
        failures += 0 if ok else 1
        speed = before_ms / elapsed if elapsed else float("inf")
        print(f"{'PASS' if ok else 'FAIL'}  {name:<32} {before_ms:>9.0f}ms -> {elapsed:>8.1f}ms  ({speed:>6.1f}x)")
        if not ok:
            print(f"      expect {want}")
            print(f"      actual {sig}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
