"""S2 — 真实吞吐实测（架构基准数字，最重要）。

连真实节点（只读 ~/.easy_tdx/config.json 的最优节点），测量：
- A. 全 A 证券清单枚举耗时与只数（不触发 ~/.easy_tdx/cache 写入）
- B. 单只 10 年日线（≈2400 根）耗时
- C. 40 只 × 5 年：串行(连接复用) / 串行(每只新连) / 并发 2/4/8（线程本地复用）
- D. 限流 / 断连 / 失败率观察
- E. 外推全 A（实测只数）× 10 年总耗时，判断「首次全量 ≤2h」是否现实
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from easy_tdx.client import TdxClient
from easy_tdx.mac.client import MacClient
from easy_tdx.models.enums import Market

from common import BEST_HOST, BEST_MAC_HOST, PORT, save_result

COUNT_10Y = 2400
COUNT_5Y = 1200
N_BATCH = 40
TIMEOUT_PER_BATCH = 900

_tls = threading.local()


def _new_mac() -> MacClient:
    c = MacClient(host=BEST_MAC_HOST, port=PORT)
    c.connect()  # 必须显式 connect，否则首个请求要走一遍重试阶梯
    return c


def _tls_client() -> MacClient:
    """线程本地 MacClient（模拟连接池：每个 worker 一条长连接）。"""
    c = getattr(_tls, "cli", None)
    if c is None:
        c = _new_mac()
        _tls.cli = c
    return c


def _pull(cli: MacClient, market: int, code: str, count: int):
    t0 = time.time()
    try:
        df = cli.get_stock_kline(market=market, code=code, period=4, count=count)
        return (code, len(df) if df is not None else 0, None, time.time() - t0)
    except Exception as e:  # noqa: BLE001
        return (code, 0, f"{type(e).__name__}: {str(e)[:100]}", time.time() - t0)


def _run_batch(pairs, workers: int, count: int, reuse: bool, label: str):
    """pairs: list[(market:int, code:str)]"""
    t0 = time.time()
    done = failed = 0
    errors: list[str] = []
    latencies: list[float] = []

    if workers == 1:
        cli = _new_mac() if reuse else None
        for market, code in pairs:
            c = cli if reuse else _new_mac()
            _, n, err, lat = _pull(c, market, code, count)
            latencies.append(lat)
            if err:
                failed += 1
                if len(errors) < 5:
                    errors.append(f"{code}: {err}")
            else:
                done += 1
            if not reuse:
                try:
                    c.close()
                except Exception:  # noqa: BLE001,S110
                    pass
        if reuse and cli is not None:
            try:
                cli.close()
            except Exception:  # noqa: BLE001,S110
                pass
    else:
        def _task(mc):
            market, code = mc
            return _pull(_tls_client(), market, code, count)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_task, mc) for mc in pairs]
            for fut in as_completed(futs, timeout=TIMEOUT_PER_BATCH):
                _, n, err, lat = fut.result()
                latencies.append(lat)
                if err:
                    failed += 1
                    if len(errors) < 5:
                        errors.append(err)
                else:
                    done += 1

    wall = time.time() - t0
    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else None
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else None
    out = {
        "label": label, "workers": workers, "reuse_conn": reuse,
        "n": len(pairs), "wall_seconds": round(wall, 2),
        "done": done, "failed": failed,
        "fail_rate": round(failed / max(1, len(pairs)), 4),
        "per_stock_seconds": round(wall / max(1, len(pairs)), 4),
        "latency_p50": round(p50, 3) if p50 else None,
        "latency_p95": round(p95, 3) if p95 else None,
        "errors_sample": errors,
    }
    print(f"[batch] {label:22s} wall={out['wall_seconds']:8.2f}s per_stock={out['per_stock_seconds']:.4f}s "
          f"done={done} failed={failed} p50={out['latency_p50']} p95={out['latency_p95']}")
    return out


def main() -> dict:
    res: dict = {}
    print("=" * 88)
    print(f"S2 — 真实吞吐实测  tdx节点={BEST_HOST}:{PORT}  mac节点={BEST_MAC_HOST}:{PORT}")
    print("=" * 88)

    # ── A0. 关键陷阱实测：GetSecurityListCmd 在同一条连接上复用会超时 15s
    #      （config timeout=15s；服务端对同连接的第 2 次 security_list 不响应）
    try:
        probe = {"reuse": [], "fresh": []}
        c = TdxClient(BEST_HOST, PORT)
        c.connect()
        for s in (0, 1000, 2000):
            t0 = time.time()
            c.get_security_list(Market.SH, s)
            probe["reuse"].append(round(time.time() - t0, 2))
        c.close()
        for s in (0, 1000, 2000):
            cc = TdxClient(BEST_HOST, PORT)
            cc.connect()
            t0 = time.time()
            cc.get_security_list(Market.SH, s)
            probe["fresh"].append(round(time.time() - t0, 2))
            cc.close()
        probe["verdict"] = (
            "同连接复用 security_list 从第 2 页起每页 ≈15s（read timeout 后重连），"
            "每页新建连接则 ≈0.05s —— 全 A 枚举必须每页新连接"
        )
        res["A0_security_list_conn_trap"] = probe
        print(f"[A0] security_list 复用连接耗时={probe['reuse']}s vs 每页新连接={probe['fresh']}s")
    except Exception as e:  # noqa: BLE001
        res["A0_security_list_conn_trap"] = {"error": f"{type(e).__name__}: {e}"}

    # ── A. 全 A 证券清单（每页新建连接；不调用 get_security_list_all 以免写 ~/.easy_tdx/cache）
    pairs: list[tuple[int, str]] = []
    try:
        t0 = time.time()
        counts = {}
        rows = []
        for mk in (Market.SH, Market.SZ):
            c0 = TdxClient(BEST_HOST, PORT)
            c0.connect()
            cnt = c0.get_security_count(mk)
            c0.close()
            counts[mk.name] = cnt
            for start in range(0, cnt, 1000):
                cc = TdxClient(BEST_HOST, PORT)
                cc.connect()
                try:
                    df = cc.get_security_list(mk, start)
                finally:
                    cc.close()
                if df is None or df.empty:
                    continue
                rows.append((mk, df))
        elapsed = time.time() - t0
        a_codes: list[tuple[int, str]] = []
        for mk, df in rows:
            for code in df["code"].tolist():
                is_a = (mk == Market.SH and str(code).startswith(("60", "68"))) or (
                    mk == Market.SZ and str(code).startswith(("00", "30")))
                if is_a:
                    a_codes.append((int(mk), str(code)))
        res["universe"] = {"ok": True, "seconds": round(elapsed, 2),
                           "pages": len(rows), "security_count": counts,
                           "a_share_count": len(a_codes),
                           "sample": [c for _, c in a_codes[:5]]}
        print(f"[A] 全市场证券数={counts} → A股={len(a_codes)} 只，"
              f"{len(rows)} 页枚举耗时={elapsed:.2f}s")
        pairs = a_codes
    except Exception as e:  # noqa: BLE001
        res["universe"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(f"[A] 证券清单枚举 FAILED: {e}")

    if not pairs:
        pairs = [(1, f"60{i:04d}") for i in range(N_BATCH)]

    # ── B. 单只 10 年 × 3
    singles = []
    cli = _new_mac()
    m0, c0 = pairs[0]
    for i in range(3):
        _, n, err, lat = _pull(cli, m0, c0, COUNT_10Y)
        singles.append({"run": i, "seconds": round(lat, 3), "rows": n, "error": err})
        print(f"[B] single {c0} run{i}: {lat:.3f}s rows={n} err={err}")
    try:
        cli.close()
    except Exception:  # noqa: BLE001,S110
        pass
    ok = [s["seconds"] for s in singles if s["error"] is None]
    per_10y = sum(ok) / len(ok) if ok else None
    rows_10y = max((s["rows"] for s in singles), default=0)
    res["single_10y"] = {"per_stock_seconds": per_10y, "rows": rows_10y, "runs": singles}
    print(f"[B] 单只 10 年平均 = {per_10y}s（{rows_10y} 根）")

    # ── C. 批量 40 只 × 5 年
    batch = pairs[:N_BATCH]
    res["batch"] = {}
    res["batch"]["serial_reuse"] = _run_batch(batch, 1, COUNT_5Y, True, "serial(复用连接)")
    res["batch"]["serial_newconn"] = _run_batch(batch, 1, COUNT_5Y, False, "serial(每只新连接)")
    for w in (2, 4, 8):
        res["batch"][f"conc{w}"] = _run_batch(batch, w, COUNT_5Y, True, f"concurrent(workers={w})")

    # ── F. 真实工况抽样：300 只 × 10 年 @ workers=4（40 只样本太小，噪声大）
    big = pairs[:300] if len(pairs) >= 300 else pairs
    res["batch_300_10y_w4"] = _run_batch(big, 4, COUNT_10Y, True, "300只×10年 workers=4")

    # ── E. 外推
    total_a = res.get("universe", {}).get("a_share_count") or 5400
    sr = res["batch"]["serial_reuse"]
    base_per_stock_5y = sr["per_stock_seconds"]
    # 10 年 ≈ 3 页 vs 5 年 ≈ 2 页，用单只实测的 10y/5y 比例修正
    ratio = (per_10y / base_per_stock_5y) if (per_10y and base_per_stock_5y) else 1.5
    per_stock_10y = per_10y or base_per_stock_5y * 1.5
    serial_total = per_stock_10y * total_a
    speedups = {}
    for w in (2, 4, 8):
        b = res["batch"][f"conc{w}"]
        speedups[w] = round(sr["wall_seconds"] / b["wall_seconds"], 2) if b["wall_seconds"] else None
    best_w = max((w for w in (2, 4, 8) if speedups[w]), key=lambda w: speedups[w], default=None)
    best_speedup = speedups.get(best_w) if best_w else None
    conc_total = serial_total / best_speedup if best_speedup else None

    # 以 300 只真实工况为基准的外推（比单只外推更可信）
    b300 = res["batch_300_10y_w4"]
    measured_rate = b300["n"] / b300["wall_seconds"] if b300["wall_seconds"] else None  # 只/秒
    full_a_seconds_measured = total_a / measured_rate if measured_rate else None

    res["extrapolation"] = {
        "a_share_count_measured": total_a,
        "per_stock_10y_seconds": round(per_stock_10y, 4),
        "serial_total_hours": round(serial_total / 3600, 2),
        "speedups": speedups,
        "best_workers": best_w,
        "best_speedup": best_speedup,
        "concurrent_total_hours": round(conc_total / 3600, 2) if conc_total else None,
        "measured_rate_stocks_per_sec_300x10y_w4": round(measured_rate, 2) if measured_rate else None,
        "full_a_10y_seconds_from_300_sample": round(full_a_seconds_measured, 1) if full_a_seconds_measured else None,
        "full_a_10y_minutes_from_300_sample": round(full_a_seconds_measured / 60, 1) if full_a_seconds_measured else None,
        "prd_target_hours": 2.0,
        "feasible_2h": bool(full_a_seconds_measured and full_a_seconds_measured <= 7200),
        "ratio_10y_over_5y": round(ratio, 2),
        "note": "主口径 = 300只×10年@workers=4 实测速率 × 实测A股只数；未含写盘耗时（见 S3 步骤9）",
    }
    if full_a_seconds_measured:
        print(f"[E*] 300只实测速率={measured_rate:.2f} 只/秒 → 全A({total_a}) × 10年 拉取 ≈ "
              f"{full_a_seconds_measured:.0f}s = {full_a_seconds_measured/60:.1f} 分钟")
    print(f"[E] 全A={total_a} 只 × 10年：串行 ≈ {serial_total/3600:.2f}h；"
          f"最优并发 workers={best_w} 加速比={best_speedup} → ≈ "
          f"{(conc_total/3600) if conc_total else float('nan'):.2f}h；"
          f"≤2h 可行={res['extrapolation']['feasible_2h']}")

    res["status"] = "pass" if per_10y else "fail_no_data"
    save_result("S2_throughput", res)
    return res


if __name__ == "__main__":
    main()
