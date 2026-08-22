"""Kuantix 核心链路性能基准用例集。

覆盖四条性能敏感路径：
  1. data   —— SQLite 行情读写、连接开销、写后回读校验
  2. search —— 证券清单搜索
  3. factor —— 因子库 parquet 读取
  4. screen —— 选股打分主循环

用法：
    .venv/bin/python bench/bench_Kuantix.py before   # 优化前基线
    .venv/bin/python bench/bench_Kuantix.py after    # 优化后复测
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR.parent))
sys.path.insert(0, str(BENCH_DIR))

import pandas as pd  # noqa: E402

from harness import Bench  # noqa: E402

from Kuantix.adapters.factor_bridge import L1Reader  # noqa: E402
from Kuantix.config import get_config  # noqa: E402
from Kuantix.data.market_store import MarketStore  # noqa: E402
from Kuantix.data.security_search import SecuritySearchService  # noqa: E402
from Kuantix.factor.store import FactorStore  # noqa: E402
from Kuantix.screen.service import ScreenRequest, ScreenService  # noqa: E402


def sig_bars(bars) -> str:
    """Bar 列表指纹：条数 + 首末日期 + 收盘价校验和。"""
    if not bars:
        return "empty"
    checksum = hashlib.md5(  # noqa: S324 - 非安全用途，仅做结果一致性指纹
        ";".join(f"{b.date}:{b.close:.4f}" for b in bars).encode()
    ).hexdigest()[:12]
    return f"n={len(bars)},{bars[0].date}-{bars[-1].date},{checksum}"


def sig_frame(df: pd.DataFrame) -> str:
    """DataFrame 指纹：形状 + 数值列校验和。"""
    if df is None or df.empty:
        return "empty"
    num = df.select_dtypes("number")
    checksum = hashlib.md5(  # noqa: S324
        num.round(6).to_csv(index=False).encode()
    ).hexdigest()[:12]
    return f"shape={df.shape},{checksum}"


def sig_frames_dict(d: dict) -> str:
    """{code: DataFrame} 指纹。"""
    if not d:
        return "empty"
    parts = [f"{k}:{v.shape[0]}" for k, v in sorted(d.items())]
    checksum = hashlib.md5(";".join(parts).encode()).hexdigest()[:12]  # noqa: S324
    return f"codes={len(d)},{checksum}"


def sig_hits(hits) -> str:
    """搜索结果指纹。"""
    if not hits:
        return "empty"
    return f"n={len(hits)}," + ",".join(str(h.code) for h in hits[:8])


def sig_screen(results) -> str:
    """选股结果指纹：前 10 名代码 + 分数。"""
    if not results:
        return "empty"
    head = ",".join(f"{r.code}:{r.score:.6f}" for r in results[:10])
    return f"n={len(results)},{head}"


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "before"
    bench = Bench(label)
    config = get_config()

    store = MarketStore()
    reader = L1Reader(config.paths.vipdoc, backend="auto", store=store)
    factor_store = FactorStore(config.paths.factors, config.paths.db)

    # 采样：取有数据的代码
    with store._connect() as conn:  # noqa: SLF001 - 基准脚本直连取样本
        codes = [
            r[0]
            for r in conn.execute(
                "SELECT code FROM daily_bars WHERE market='CN' "
                "GROUP BY code ORDER BY COUNT(*) DESC LIMIT 400"
            ).fetchall()
        ]
    long_code = codes[0]
    print(f"样本: {len(codes)} 只代码，最长历史 = {long_code}")

    # ================================================================== #
    # 组 1：SQLite 行情读取
    # ================================================================== #
    print("\n--- data: SQLite 行情读取 ---")

    bench.run(
        "data.read_daily_bars.single_full",
        lambda: store.read_daily_bars("CN", long_code),
        group="data", scale="单只全历史 ~5200 根", repeat=5,
        signature=sig_bars,
        note="写后回读校验 _verify_daily_tail 走的就是这条路径",
    )

    bench.run(
        "data.read_tail_60",
        lambda: store.read_daily_bars("CN", long_code)[-60:],
        group="data", scale="取末 60 根（当前实现全量读后切片）", repeat=5,
        signature=sig_bars,
        note="同步写盘校验的真实语义：只需要末尾 N 条",
    )

    bench.run(
        "data.read_daily_frames.batch200",
        lambda: store.read_daily_frames(codes[:200], "CN"),
        group="data", scale="200 只 × 全历史", repeat=3,
        signature=sig_frames_dict,
    )

    bench.run(
        "data.read_daily_frame.loop200",
        lambda: {c: reader.read_daily_frame("sh", c) for c in codes[:200]},
        group="data", scale="200 只逐只读（对照批量）", repeat=3,
        signature=sig_frames_dict,
    )

    bench.run(
        "data.connect_overhead.300calls",
        lambda: [store.has_data("CN", c) for c in codes[:300]],
        group="data", scale="300 次小查询（连接开销）", repeat=3,
        signature=lambda r: f"n={len(r)},true={sum(bool(x) for x in r)}",
    )

    # ================================================================== #
    # 组 2：证券搜索
    # ================================================================== #
    print("\n--- search: 证券清单搜索 ---")

    def fresh_search(q: str):
        svc = SecuritySearchService(config, store=store)
        return svc.search(q, "CN", limit=20)

    bench.run(
        "search.cold_by_code",
        lambda: fresh_search("600000"),
        group="search", scale="冷启动按代码（17634 条清单）", repeat=5,
        signature=sig_hits,
    )

    bench.run(
        "search.cold_by_name",
        lambda: fresh_search("银行"),
        group="search", scale="冷启动按名称", repeat=5,
        signature=sig_hits,
    )

    warm_svc = SecuritySearchService(config, store=store)
    warm_svc.search("600000", "CN", limit=20)
    bench.run(
        "search.warm_by_code",
        lambda: warm_svc.search("600519", "CN", limit=20),
        group="search", scale="热缓存按代码", repeat=10,
        signature=sig_hits,
    )

    # ================================================================== #
    # 组 3：因子库读取
    # ================================================================== #
    print("\n--- factor: 因子库 parquet 读取 ---")

    factors = [p.name for p in sorted(Path(config.paths.factors).iterdir()) if p.is_dir()]
    big_factor = "momentum_60d" if "momentum_60d" in factors else factors[0]

    with store._connect() as conn:  # noqa: SLF001
        max_date = int(conn.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0])

    fdf = factor_store.load(big_factor)
    latest_factor_date = int(fdf["date"].max()) if not fdf.empty else max_date
    del fdf
    print(f"因子最新日期: {latest_factor_date}")

    bench.run(
        "factor.load_single_date",
        lambda: factor_store.load(big_factor, date=latest_factor_date),
        group="factor", scale=f"{big_factor} 单日截面（库 82MB/6 年）", repeat=3,
        signature=sig_frame,
        note="当前实现读全部年份 parquet 后内存过滤",
    )

    bench.run(
        "factor.load_year_range",
        lambda: factor_store.load(big_factor, start=20250101, end=20251231),
        group="factor", scale=f"{big_factor} 单年区间", repeat=3,
        signature=sig_frame,
    )

    bench.run(
        "factor.load_single_code",
        lambda: factor_store.load(big_factor, code=long_code),
        group="factor", scale=f"{big_factor} 单只全历史", repeat=3,
        signature=sig_frame,
    )

    multi = factors[:5]
    bench.run(
        "factor.load_5factors_single_date",
        lambda: [factor_store.load(f, date=latest_factor_date) for f in multi],
        group="factor", scale="5 因子 × 单日截面（选股入口真实形态）", repeat=3,
        signature=lambda fs: ";".join(sig_frame(f) for f in fs),
    )

    # ================================================================== #
    # 组 4：选股主循环
    # ================================================================== #
    print("\n--- screen: 选股打分主循环 ---")

    screen = ScreenService(config, store=factor_store, reader=reader)

    bench.run(
        "screen.factor_top50_nocond",
        lambda: screen.screen_factor(
            factor=big_factor, market="CN", top_n=50, order="desc",
            as_of=None, tech_cond={}, chanlun_cond={},
        ),
        group="screen", scale="单因子 top50 无过滤条件（全市场）", repeat=1, warmup=0,
        signature=lambda r: sig_screen(r[0]) if isinstance(r, tuple) else sig_screen(r),
        note="无条件时仍逐只读全历史日线，最大浪费点",
    )

    bench.run(
        "screen.factor_top50_techcond",
        lambda: screen.screen_factor(
            factor=big_factor, market="CN", top_n=50, order="desc",
            as_of=None, tech_cond={"min_close": 5.0}, chanlun_cond={},
        ),
        group="screen", scale="单因子 top50 + 技术过滤（全市场）", repeat=1, warmup=0,
        signature=lambda r: sig_screen(r[0]) if isinstance(r, tuple) else sig_screen(r),
    )

    req = ScreenRequest(market="CN", factors=tuple(multi), top_n=50)
    bench.run(
        "screen.multifactor_top50",
        lambda: screen.run(req),
        group="screen", scale="5 因子等权 top50（全市场）", repeat=1, warmup=0,
        signature=sig_screen,
    )

    out = BENCH_DIR / f"baseline_{label}.json"
    bench.save(out)


if __name__ == "__main__":
    main()
