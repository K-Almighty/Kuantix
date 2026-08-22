"""S3 — vipdoc 数据湖端到端闭环（数据层地基，命门，必须跑通）。

步骤：
1. 建 ~/.Kuantix/vipdoc/sh/lday/ 目录（无本地通达信安装）
2. 在线拉 600000 真实日线
3. sync_daily_bars_from_security_bars 写入 .day
4. read_daily_bars 读回
5. 逐字段比对写入前/读回 OHLCV（验证 RD-1 系数一致性，价格不能错 10 倍）
5b. RD-1 反面验证：故意用错系数写 ETF(sh510300)，证明会读出 10 倍错价
6. SignalScanner(vipdoc_path=...) 扫描，确认扫到 600000 并出信号
7. 重复写入验证增量去重
8. 模拟崩溃残尾，验证 _repair_tail 自愈
9. 写盘吞吐：批量写 N 只，外推全 A 落盘耗时与磁盘占用

注意：本 spikes/ 目录下所有脚本均为一次性技术验证（Spike），其代码风格与写法
**不作为 Kuantix 正式编码规范参考**；其中含故意错配的反面样本（如 5b 段的
(0.01, 0.01) 系数），仅用于复现陷阱，严禁照抄进业务代码（NF-25/NF-26）。
"""
from __future__ import annotations

import datetime as _dt
import time
from pathlib import Path

from easy_tdx.backtest.strategy import Strategy
from easy_tdx.mac.client import MacClient
from easy_tdx.models.bar import SecurityBar
from easy_tdx.offline.daily_bar import (
    _SECURITY_COEFFICIENTS,
    _detect_security_type,
    read_daily_bars,
)
from easy_tdx.offline.write_daily import (
    append_daily_bars,
    get_last_bar_date,
    sync_daily_bars_from_security_bars,
)
from easy_tdx.screen.scanner import SignalScanner

from common import BEST_MAC_HOST, PORT, VIPDOC_ROOT, df_to_security_bars, save_result

MARKET_SH = 1
CODE = "600000"
PRICE_COEFF, VOL_COEFF = _SECURITY_COEFFICIENTS["SH_A_STOCK"]  # (0.01, 0.01)


class AlwaysBuyLast(Strategy):
    """最小策略：最后一根 bar 买入，让 SignalScanner 必定产出信号。

    注意 API 惯例（S5 已实测）：指标/持仓判断要用 self._bar_index 与
    self.position["size"]，不能用 [-1] 或 truthy 判断。
    """

    def init(self):
        self._n = len(self.data.close)

    def next(self):
        if self._bar_index >= self._n - 1 and self.position["size"] == 0:
            self.buy(size=0)


def _max_rel_diff(a, b) -> float:
    m = 0.0
    for x, y in zip(a, b):
        denom = max(abs(x), 1e-9)
        m = max(m, abs(x - y) / denom)
    return m


def main() -> dict:
    res: dict = {"steps": {}}
    print("=" * 88)
    print("S3 — vipdoc 数据湖端到端闭环（无本地通达信安装）")
    print("=" * 88)

    # ── 1. 目录
    lday = VIPDOC_ROOT / "sh" / "lday"
    lday.mkdir(parents=True, exist_ok=True)
    day_file = lday / f"sh{CODE}.day"
    if day_file.exists():
        day_file.unlink()
    res["steps"]["1_dir"] = {"vipdoc": str(VIPDOC_ROOT), "lday": str(lday),
                             "local_tdx_install": False}
    print(f"[1] vipdoc 根目录 = {VIPDOC_ROOT}（纯自建，无通达信安装）")

    # ── 2. 在线拉取
    t0 = time.time()
    client = MacClient(host=BEST_MAC_HOST, port=PORT)
    df = client.get_stock_kline(market=MARKET_SH, code=CODE, period=4, count=2400)
    pull_s = time.time() - t0
    n_rows = len(df)
    res["steps"]["2_pull"] = {"rows": n_rows, "seconds": round(pull_s, 3),
                              "columns": list(df.columns)}
    print(f"[2] 在线拉取 {CODE}: {n_rows} 行 / {pull_s:.2f}s, columns={list(df.columns)}")
    if n_rows < 30:
        res["status"] = "abort"
        res["reason"] = "在线拉取数据不足"
        save_result("S3_vipdoc", res)
        return res

    # ── 2b. RD-8 实测：vol 单位不匹配会撑爆 uint32
    from easy_tdx.offline.write_daily import encode_daily_bar as _enc
    raw_bars = df_to_security_bars(df, vol_divisor=1.0)  # 不转换：直接用「股」
    try:
        _enc(raw_bars[int(df["vol"].idxmax())], PRICE_COEFF, VOL_COEFF)
        rd8 = {"overflow": False}
    except Exception as e:  # noqa: BLE001
        rd8 = {"overflow": True, "error": f"{type(e).__name__}: {e}"}
    max_vol_shares = float(df["vol"].max())
    rd8.update({
        "mac_vol_unit": "股（实测 amount/close ≈ vol）",
        "vipdoc_write_expects": "手（stored_uint32 = vol/vol_coeff = 手×100 = 股）",
        "max_vol_shares_sample": max_vol_shares,
        "encoded_if_no_convert": max_vol_shares / VOL_COEFF,
        "uint32_max": 4294967295,
        "headroom_ratio_after_convert": round(4294967295 / (max_vol_shares / 100 / VOL_COEFF), 1),
    })
    res["steps"]["2b_rd8_vol_unit"] = rd8
    print(f"[2b] RD-8 vol 单位：不换算直接编码 → overflow={rd8['overflow']}；"
          f"样本最大量={max_vol_shares:.0f}股，不换算编码值={max_vol_shares/VOL_COEFF:.3e} > uint32max；"
          f"换算(÷100)后余量={rd8['headroom_ratio_after_convert']}×")

    # ── 3. 写入 vipdoc（vol 股→手）
    bars = df_to_security_bars(df)
    t0 = time.time()
    written = sync_daily_bars_from_security_bars(day_file, bars, PRICE_COEFF, VOL_COEFF)
    write_s = time.time() - t0
    size = day_file.stat().st_size
    res["steps"]["3_write"] = {"written": written, "file_size": size,
                               "seconds": round(write_s, 4),
                               "bytes_per_bar": size // max(1, written)}
    print(f"[3] 写入 {day_file.name}: {written} 条 / {write_s*1000:.1f}ms, size={size}B "
          f"({size//max(1,written)}B/根)")

    # ── 4. 读回
    t0 = time.time()
    read_back = read_daily_bars(day_file)
    read_s = time.time() - t0
    res["steps"]["4_read"] = {"read": len(read_back), "seconds": round(read_s, 4)}
    print(f"[4] 读回: {len(read_back)} 条 / {read_s*1000:.1f}ms")

    # ── 5. 逐字段比对（RD-1 正面）
    fields = ["open", "close", "high", "low", "vol", "amount"]
    cmp = {}
    for fld in fields:
        a = [getattr(x, fld) for x in bars]
        b = [getattr(x, fld) for x in read_back]
        cmp[fld] = {
            "max_rel_diff": round(_max_rel_diff(a[: len(b)], b), 8),
            "sample_wrote": a[0], "sample_read": b[0],
        }
    price_diff = max(cmp[f]["max_rel_diff"] for f in ("open", "close", "high", "low"))
    rd1_ok = price_diff < 1e-6
    # 在线口径(股) vs vipdoc 读回口径(手) 的 100 倍语义差，必须由 Kuantix 数据层统一
    cmp["_vol_semantics"] = {
        "online_df_vol_first": float(df["vol"].iloc[0]),
        "vipdoc_read_vol_first": read_back[0].vol,
        "ratio_online_over_vipdoc": round(float(df["vol"].iloc[0]) / max(read_back[0].vol, 1e-9), 2),
        "note": "在线=股，vipdoc读回=手；Kuantix 必须在数据层统一口径",
    }
    res["steps"]["5_compare"] = {"per_field": cmp, "price_max_rel_diff": price_diff,
                                 "RD1_ok": rd1_ok}
    print(f"[5] 字段比对 价格 max_rel_diff={price_diff:.2e} vol={cmp['vol']['max_rel_diff']:.2e} "
          f"amount={cmp['amount']['max_rel_diff']:.2e} → RD-1 正面 OK={rd1_ok}")
    print(f"    样例 close 写入={cmp['close']['sample_wrote']:.4f} 读回={cmp['close']['sample_read']:.4f}")
    if not rd1_ok:
        res["status"] = "fail_rd1"
        save_result("S3_vipdoc", res)
        return res

    # ── 5b. RD-1 反面：ETF 用股票系数写 → 读回必错 10 倍
    etf_file = lday / "sh510300.day"
    if etf_file.exists():
        etf_file.unlink()
    # !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    # !! 故意错配的反面样本（T1 系数陷阱复现，切勿照抄！）
    # !! 此行 (0.01, 0.01) 是 A 股系数，但 etf_file = sh510300 实为 ETF，
    # !! 正确系数应为 (0.001, 1.0)。用错系数写盘后读回价格会被缩小 ×0.10，
    # !! 这正是 PRD 头号数据损坏风险（NF-25 编解码对称）。
    # !! 本行用途仅限「复现陷阱 + 验证红线检查器 R1 能抓到」，有存档价值。
    # !! 正式代码必须经 adapters/coefficients.py 从上游 import 获取系数（NF-25），
    # !! 严禁复制本行字面量。
    # !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    etf_bars = bars[-100:]
    sync_daily_bars_from_security_bars(etf_file, etf_bars, 0.01, 0.01)  # 错误：ETF 应为 0.001
    etf_read = read_daily_bars(etf_file)
    etf_type = _detect_security_type(etf_file.name)
    etf_coeff = _SECURITY_COEFFICIENTS[etf_type]
    ratio = etf_read[0].close / etf_bars[0].close if etf_bars[0].close else 0
    res["steps"]["5b_rd1_negative"] = {
        "file": etf_file.name, "detected_type": etf_type, "read_coeff": etf_coeff,
        "wrote_coeff": [0.01, 0.01], "wrote_close": etf_bars[0].close,
        "read_close": etf_read[0].close, "ratio_read_over_wrote": round(ratio, 4),
        "trap_confirmed": abs(ratio - 0.1) < 0.01,
    }
    print(f"[5b] RD-1 反面：{etf_file.name} 识别为 {etf_type} 读系数={etf_coeff}，"
          f"用 0.01 写 → 写入 {etf_bars[0].close:.3f} 读回 {etf_read[0].close:.3f} "
          f"(×{ratio:.2f}) 陷阱确认={res['steps']['5b_rd1_negative']['trap_confirmed']}")
    etf_file.unlink()

    # ── 6. SignalScanner 扫描
    t0 = time.time()
    scanner = SignalScanner(strategy_cls=AlwaysBuyLast, vipdoc_path=str(VIPDOC_ROOT))
    scanned = scanner.scan(universe="sh", workers=0)
    scan_s = time.time() - t0
    hit = any(r.code == CODE for r in scanned)
    res["steps"]["6_scan"] = {
        "total_signals": len(scanned), "hit_600000": hit,
        "seconds": round(scan_s, 3),
        "sample": [f"{r.market}{r.code}@{r.signal_date} close={r.last_close}" for r in scanned[:5]],
    }
    print(f"[6] SignalScanner(vipdoc_path=自建目录) 扫描: {len(scanned)} 信号 / {scan_s:.2f}s，"
          f"命中 {CODE}={hit}")
    if not hit:
        res["status"] = "fail_scan"
        save_result("S3_vipdoc", res)
        return res

    # ── 7. 重复写入去重
    before = len(read_daily_bars(day_file))
    rewritten = sync_daily_bars_from_security_bars(day_file, bars, PRICE_COEFF, VOL_COEFF)
    after = len(read_daily_bars(day_file))
    dedup_ok = (rewritten == 0) and (before == after)
    res["steps"]["7_dedup"] = {"before": before, "rewritten": rewritten, "after": after,
                               "ok": dedup_ok}
    print(f"[7] 重复写同一批: 实写 {rewritten} 条, {before}->{after}, 幂等 OK={dedup_ok}")

    # ── 8. 崩溃残尾自愈
    last = read_back[-1]
    d = _dt.date(last.year, last.month, last.day) + _dt.timedelta(days=1)
    new_bar = SecurityBar(open=last.open, close=last.close, high=last.high, low=last.low,
                          vol=last.vol, amount=last.amount,
                          year=d.year, month=d.month, day=d.day, hour=0, minute=0)
    with open(day_file, "ab") as f:
        f.write(b"\x00" * 15)  # 模拟写一半被 kill
    corrupt_size = day_file.stat().st_size
    last_date_before = get_last_bar_date(day_file)  # 纯读，不应修改文件
    size_after_read = day_file.stat().st_size
    appended = append_daily_bars(day_file, [new_bar], PRICE_COEFF, VOL_COEFF)
    final_size = day_file.stat().st_size
    repair_ok = (corrupt_size % 32 == 15) and (size_after_read == corrupt_size) \
        and (final_size % 32 == 0) and (appended == 1)
    res["steps"]["8_crash_repair"] = {
        "corrupt_tail_bytes": corrupt_size % 32,
        "get_last_bar_date_no_side_effect": size_after_read == corrupt_size,
        "last_date_skipping_corrupt": last_date_before,
        "appended_after_repair": appended,
        "final_size_mod32": final_size % 32,
        "final_bars": len(read_daily_bars(day_file)),
        "ok": repair_ok,
    }
    print(f"[8] 崩溃自愈: 残尾 {corrupt_size%32}B → get_last_bar_date 只读不改={size_after_read==corrupt_size}，"
          f"append 后 size%32={final_size%32} 实写={appended} OK={repair_ok}")

    # ── 9. 写盘吞吐外推
    n_probe = 20
    probe_dir = VIPDOC_ROOT / "sz" / "lday"
    probe_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    total_bytes = 0
    for i in range(n_probe):
        p = probe_dir / f"sz{i:06d}.day"
        if p.exists():
            p.unlink()
        sync_daily_bars_from_security_bars(p, bars, PRICE_COEFF, VOL_COEFF)
        total_bytes += p.stat().st_size
    write_probe_s = time.time() - t0
    per_file = write_probe_s / n_probe
    for i in range(n_probe):
        (probe_dir / f"sz{i:06d}.day").unlink(missing_ok=True)
    a_total = 5400
    res["steps"]["9_write_throughput"] = {
        "probe_files": n_probe, "bars_each": len(bars),
        "seconds_total": round(write_probe_s, 3),
        "seconds_per_file": round(per_file, 4),
        "bytes_per_file": total_bytes // n_probe,
        "extrapolate_full_a_write_seconds": round(per_file * a_total, 1),
        "extrapolate_full_a_disk_mb": round(total_bytes / n_probe * a_total / 1024 / 1024, 1),
    }
    print(f"[9] 写盘吞吐: {n_probe} 只 × {len(bars)} 根 = {write_probe_s:.2f}s "
          f"({per_file*1000:.1f}ms/只) → 全A({a_total}) 落盘 ≈ {per_file*a_total:.0f}s，"
          f"磁盘 ≈ {total_bytes/n_probe*a_total/1024/1024:.0f}MB")

    # ── 10. uint32 余量风险量化：全市场抽样最大单日成交量
    try:
        from easy_tdx.client import TdxClient
        from easy_tdx.models.enums import Market as _Mk
        tcc = TdxClient("180.153.18.170", PORT)
        tcc.connect()
        sh_list = tcc.get_security_list(_Mk.SH, 0)
        tcc.close()
        tcc2 = TdxClient("180.153.18.170", PORT)
        tcc2.connect()
        sz_list = tcc2.get_security_list(_Mk.SZ, 0)
        tcc2.close()
        sample = [(1, str(c)) for c in sh_list["code"].tolist() if str(c).startswith(("60", "68"))][:100]
        sample += [(0, str(c)) for c in sz_list["code"].tolist() if str(c).startswith(("00", "30"))][:100]
        worst = {"vol_shares": 0.0, "code": None}
        c2 = MacClient(host=BEST_MAC_HOST, port=PORT)
        c2.connect()
        for mk, cd in sample:
            try:
                d2 = c2.get_stock_kline(market=mk, code=cd, period=4, count=2400)
                if d2 is not None and len(d2):
                    mv = float(d2["vol"].max())
                    if mv > worst["vol_shares"]:
                        worst = {"vol_shares": mv, "code": cd}
            except Exception:  # noqa: BLE001,S112
                continue
        c2.close()
        encoded = worst["vol_shares"] / 100 / VOL_COEFF  # 手 → stored uint32
        res["steps"]["10_uint32_headroom"] = {
            "sampled_stocks": len(sample),
            "worst_code": worst["code"],
            "worst_daily_vol_shares": worst["vol_shares"],
            "encoded_uint32_value": encoded,
            "uint32_max": 4294967295,
            "headroom_ratio": round(4294967295 / max(encoded, 1), 1),
            "safe": encoded < 4294967295,
        }
        print(f"[10] uint32 余量：抽样 {len(sample)} 只 × 10 年，最大单日量 = "
              f"{worst['vol_shares']:.0f} 股（{worst['code']}）→ 编码值 {encoded:.3e}，"
              f"余量 {4294967295/max(encoded,1):.1f}× 安全={encoded < 4294967295}")
    except Exception as e:  # noqa: BLE001
        res["steps"]["10_uint32_headroom"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        print(f"[10] uint32 余量抽样失败: {e}")

    try:
        client.close()
    except Exception:  # noqa: BLE001,S110
        pass

    res["status"] = "pass" if (rd1_ok and hit and dedup_ok and repair_ok) else "partial"
    save_result("S3_vipdoc", res)
    print(f"\nS3 status = {res['status']}")
    return res


if __name__ == "__main__":
    main()
