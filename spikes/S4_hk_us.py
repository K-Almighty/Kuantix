"""S4 — 港美股可用性探测（决定 P1 排期，对应 PRD 风险 N4）。

关键更正（勘察阶段的错误假设）：港美股**不能**用 MacClient(7709) + ExMarket 代码，
必须用 easy_tdx.ex.mac_client.MacExClient（端口 7727，连接后需 Login 握手），
接口是 goods_kline(market, code, period, ...) 而非 get_stock_kline。

本 spike 实测：
1. mac_ex_hosts 连通性（只读 config，不做 from_best_host 以免写盘）
2. goods_list 枚举港股/美股标的数量
3. goods_kline 取 00700 / AAPL 日线：条数、历史深度、字段
4. sync_ex_daily_bars → read_ex_daily_bars 写读回闭环（ExDailyBar 格式）
结论：可行 / 需变更实现路径 / 不可行。
"""
from __future__ import annotations

import socket
import time
import traceback

import pandas as pd

from easy_tdx.ex.mac_client import MacExClient
from easy_tdx.mac.enums import ExMarket, Period
from easy_tdx.offline.ex_daily_bar import read_ex_daily_bars
from easy_tdx.offline.write_ex_daily import sync_ex_daily_bars

from common import EX_PORT, MAC_EX_HOSTS, VIPDOC_ROOT, df_to_ex_daily_bars, save_result

COUNT = 800


def _reachable(host: str, port: int, timeout: float = 5.0):
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True, round((time.time() - t0) * 1000, 1)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def probe(client: MacExClient, name: str, market: ExMarket, codes: list[str]) -> dict:
    out: dict = {"market_name": name, "market_code": int(market)}

    # 标的清单
    try:
        t0 = time.time()
        lst = client.goods_list(int(market), start=0, count=50)
        out["goods_list"] = {
            "ok": lst is not None and not lst.empty,
            "sample_rows": 0 if lst is None or lst.empty else len(lst),
            "seconds": round(time.time() - t0, 2),
            "sample": [] if lst is None or lst.empty
            else lst.head(5).to_dict("records"),
        }
    except Exception as e:  # noqa: BLE001
        out["goods_list"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}

    # K 线
    got = None
    tried = []
    for code in codes:
        try:
            t0 = time.time()
            df = client.goods_kline(int(market), code, Period.DAILY, start=0, count=COUNT)
            tried.append({"code": code, "rows": 0 if df is None else len(df),
                          "seconds": round(time.time() - t0, 2)})
            if df is not None and len(df) > 0:
                got = (df, code)
                break
        except Exception as e:  # noqa: BLE001
            tried.append({"code": code, "error": f"{type(e).__name__}: {str(e)[:120]}"})
    out["kline_attempts"] = tried

    if got is None:
        out["status"] = "no_data"
        return out

    df, code = got
    out["code_used"] = code
    out["rows"] = len(df)
    out["columns"] = list(df.columns)
    out["depth_years"] = round(len(df) / 250.0, 1)
    try:
        if isinstance(df.index, pd.DatetimeIndex):
            out["date_range"] = [str(df.index[0].date()), str(df.index[-1].date())]
        elif "datetime" in df.columns:
            out["date_range"] = [str(df["datetime"].iloc[0]), str(df["datetime"].iloc[-1])]
    except Exception:  # noqa: BLE001,S110
        pass
    out["head"] = df.head(2).to_dict("records")

    # 历史深度：用 start 分页回溯（服务端单次上限实测 700 根，即使 count=800）
    depth = {"page_rows": [], "earliest": None, "total": 0, "page_cap": len(df)}
    for start in (0, 700, 1400, 2100, 2800):
        try:
            d2 = client.goods_kline(int(market), code, Period.DAILY, start=start, count=800)
            n = 0 if d2 is None else len(d2)
            if n == 0:
                break
            first = (str(d2.index[0].date()) if isinstance(d2.index, pd.DatetimeIndex)
                     else str(d2["datetime"].iloc[0])[:10])
            depth["page_rows"].append({"start": start, "rows": n, "first": first})
            depth["total"] += n
            depth["earliest"] = first
        except Exception as e:  # noqa: BLE001
            depth["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            break
    depth["approx_years"] = round(depth["total"] / 250.0, 1)
    out["depth_probe"] = depth

    # 写读回闭环（ExDailyBar 格式）
    try:
        ds_dir = VIPDOC_ROOT / "ds" / "lday"
        ds_dir.mkdir(parents=True, exist_ok=True)
        fpath = ds_dir / f"{name.lower()}_{code}.day"
        if fpath.exists():
            fpath.unlink()
        bars = df_to_ex_daily_bars(df)
        w = sync_ex_daily_bars(fpath, bars)
        rb = read_ex_daily_bars(fpath)
        price_ok = abs(rb[0].close - bars[0].close) < 1e-3 if rb else False
        out["writeread"] = {
            "written": w, "read": len(rb), "count_ok": (w == len(bars) == len(rb)),
            "wrote_close": bars[0].close if bars else None,
            "read_close": rb[0].close if rb else None,
            "price_ok": price_ok,
            "file_size": fpath.stat().st_size,
            "bytes_per_bar": fpath.stat().st_size // max(1, len(rb)),
        }
        out["status"] = "available" if (w == len(bars) and price_ok) else "available_write_issue"
        fpath.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        out["writeread_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out["status"] = "available_readonly"
    return out


def main() -> dict:
    print("=" * 88)
    print(f"S4 — 港美股可用性探测（MacExClient, port {EX_PORT}）")
    print("=" * 88)
    res: dict = {"hosts": {}}

    alive = []
    for h in MAC_EX_HOSTS:
        ok, info = _reachable(h, EX_PORT)
        res["hosts"][h] = {"reachable": ok, "info": info}
        print(f"[host] {h}:{EX_PORT} reachable={ok} {info}")
        if ok:
            alive.append(h)

    if not alive:
        res["status"] = "no_reachable_ex_host"
        res["conclusion"] = "所有 mac_ex_hosts 均不可达，港美股 P1 需另寻数据源"
        save_result("S4_hk_us", res)
        print(res["conclusion"])
        return res

    host = alive[0]
    res["host_used"] = host
    client = None
    try:
        client = MacExClient(host=host, port=EX_PORT, timeout=20.0)
        client.connect()
        try:
            total = client.goods_count()
            res["goods_count_total"] = total
            print(f"[info] 扩展市场商品总数 = {total}")
        except Exception as e:  # noqa: BLE001
            res["goods_count_error"] = f"{type(e).__name__}: {str(e)[:150]}"

        res["HK"] = probe(client, "HK", ExMarket.HK_MAIN_BOARD, ["00700", "0700", "700"])
        res["US"] = probe(client, "US", ExMarket.US_STOCK, ["AAPL", "AAPL.O", ".AAPL", "AAPL.OQ"])
    except Exception as e:  # noqa: BLE001
        res["fatal"] = f"{type(e).__name__}: {e}"
        res["traceback"] = traceback.format_exc()[-800:]
        print(f"[fatal] {e}")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001,S110
                pass

    hk_ok = res.get("HK", {}).get("status") == "available"
    us_ok = res.get("US", {}).get("status") == "available"
    res["status"] = "pass" if (hk_ok or us_ok) else "fail"
    res["conclusion"] = (
        f"HK={res.get('HK', {}).get('status')} / US={res.get('US', {}).get('status')}；"
        f"实现路径必须走 MacExClient(7727)+goods_kline，与 A 股 MacClient(7709)+get_stock_kline 不同，"
        f"落盘格式为 ExDailyBar(<IffffIIf>) 且 append_ex_daily_bars 无 fsync（RD-2）"
    )
    print(f"[HK] status={res.get('HK', {}).get('status')} rows={res.get('HK', {}).get('rows')}")
    print(f"[US] status={res.get('US', {}).get('status')} rows={res.get('US', {}).get('rows')}")
    save_result("S4_hk_us", res)
    return res


if __name__ == "__main__":
    main()
