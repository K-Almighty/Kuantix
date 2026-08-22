"""盘前 / 盘后报告导出（JSON 字典 + Markdown 文本）。

统一约定：
- :func:`report_json_dict` 返回可 ``json.dumps`` 的 dict（所有 date/datetime
  已转 ISO 字符串，嵌套 tuple 已转 list，不抛 TypeError）；
- :func:`report_markdown` 返回 UTF-8 Markdown 文本（首行严格为
  ``# 盘前分析报告 · YYYY-MM-DD（XX）`` 或 ``# 盘后复盘报告 · ...``，
  长度 > 500 字符，避免空壳）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from Kuantix.core import contracts as C
from Kuantix.core.fail_loud import DataIntegrityError

__all__ = ["report_json_dict", "report_markdown"]


# ---------------------------------------------------------------------------
# JSON 序列化（递归 asdict + isoformat）
# ---------------------------------------------------------------------------


def _jsonify(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonify(value.to_dict())
    if hasattr(value, "value"):  # Enum fallback
        return value.value
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    # 兜底：转字符串（fail-loud 不允许 NaN / Inf）
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] report_json_dict 序列化遇到非法浮点: {value}"
            )
    return str(value)


def report_json_dict(
    report: C.PreOpenReport | C.PostCloseReport,
) -> dict[str, Any]:
    """把盘前 / 盘后报告对象转成 JSON-safe dict。"""
    if isinstance(report, (C.PreOpenReport, C.PostCloseReport)):
        return _jsonify(report.to_dict())
    raise DataIntegrityError(
        "[fail-loud/NF-26] report_json_dict 仅接受 PreOpenReport / PostCloseReport，"
        f"实际类型 {type(report).__name__}"
    )


# ---------------------------------------------------------------------------
# Markdown 导出
# ---------------------------------------------------------------------------

_TABLE_HEAD_NEWS = "| 来源 | 分类 | 重要性 | 标题 | 关联代码 | 发布时间 |"
_TABLE_ROW_NEWS_SEP = "|---|---|---|---|---|---|"

_TABLE_HEAD_FUND = "| 代码 | 名称 | 行业 | 市值(亿) | PE | PB | ROE | 等级 | 摘要 |"
_TABLE_ROW_FUND_SEP = "|---|---|---|---:|---:|---:|---:|---|---|"

_TABLE_HEAD_LIMIT = "| 代码 | 名称 | 行业 | 类型 | 收盘 | 涨跌幅 | 连板 | 原因 |"
_TABLE_ROW_LIMIT_SEP = "|---|---|---|---|---:|---:|---:|---|"


def _md_escape(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def _format_pct(value: float | int | None, *, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float)) and value != value:  # NaN
        return "-"
    return f"{float(value)*100:.{digits}f}%"


def _format_yuan(value: float | int | None, *, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float)) and value != value:
        return "-"
    # 按亿显示
    yi = float(value) / 1e8
    return f"{yi:.{digits}f}"


def _render_news_section(news_feed_summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    total = int(news_feed_summary.get("total") or 0)
    by_cat = news_feed_summary.get("by_category") or []
    lines.append("## 1. 消息面概览")
    lines.append("")
    lines.append(f"- 总条数：**{total}**")
    if by_cat:
        parts = []
        for item in by_cat:
            parts.append(f"{item.get('category','?')} {int(item.get('count',0))}")
        lines.append("- 分类计数：" + "；".join(parts))
    lines.append("")
    top_news = news_feed_summary.get("top_news") or []
    if top_news:
        lines.append("### Top 重要新闻")
        lines.append("")
        lines.append(_TABLE_HEAD_NEWS)
        lines.append(_TABLE_ROW_NEWS_SEP)
        for n in top_news:
            codes = ",".join(n.get("codes") or [])
            lines.append(
                "| {src} | {cat} | {imp} | [{title}]({url}) | {codes} | {ts} |".format(
                    src=_md_escape(n.get("source", "")),
                    cat=_md_escape(n.get("category", "")),
                    imp=int(n.get("importance") or 0),
                    title=_md_escape(n.get("title", "")),
                    url=n.get("url", "") or "#",
                    codes=_md_escape(codes),
                    ts=_md_escape(n.get("publish_ts", "")),
                )
            )
    lines.append("")
    return lines


def _render_fundamental_section(profiles: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = ["## 2. 自选基本面画像", ""]
    if not profiles:
        lines.append("_暂无自选基本面画像（或自选列表为空）。_")
        lines.append("")
        return lines
    lines.append(f"共 **{len(profiles)}** 只：")
    lines.append("")
    lines.append(_TABLE_HEAD_FUND)
    lines.append(_TABLE_ROW_FUND_SEP)
    for p in profiles[:20]:  # 避免 Markdown 过大
        summary = "；".join(p.get("summary_lines") or [])
        lines.append(
            "| {c} | {n} | {s} | {cap} | {pe} | {pb} | {roe} | {g} | {sum} |".format(
                c=_md_escape(p.get("code", "")),
                n=_md_escape(p.get("name", "")),
                s=_md_escape(p.get("sector", "")),
                cap=_format_yuan(p.get("market_cap"), digits=1),
                pe="-" if p.get("pe") is None else f"{float(p['pe']):.1f}",
                pb="-" if p.get("pb") is None else f"{float(p['pb']):.2f}",
                roe=_format_pct(p.get("roe")),
                g=_md_escape(p.get("grade", "")),
                sum=_md_escape(summary[:60] + ("…" if len(summary) > 60 else "")),
            )
        )
    if len(profiles) > 20:
        lines.append("")
        lines.append(f"> 仅展示前 20 / {len(profiles)}；完整列表请通过 JSON 端点或前端查询。")
    lines.append("")
    return lines


def _render_tech_section(scan_top: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = ["## 3. 大盘抽样技术面 Top", ""]
    if not scan_top:
        lines.append("_暂无技术扫描结果（请确保 lake 内有至少 260 日 L1 数据）。_")
        lines.append("")
        return lines
    lines.append(f"按信号数 + 趋势强度排序，展示前 **{len(scan_top)}**：")
    lines.append("")
    head = "| 代码 | MA5/MA20/MA60 | MACD柱 | RSI(14) | KDJ(J) | 趋势 | 支撑位 | 压力位 | 信号 |"
    sep = "|---|---|---|---|---|---|---|---|---|"
    lines.append(head)
    lines.append(sep)
    for t in scan_top[:30]:
        ma_str = "/".join(
            "-" if t.get(k) is None else f"{float(t[k]):.2f}"
            for k in ("ma5", "ma20", "ma60")
        )
        hist = "-" if t.get("macd_hist_last") is None else f"{float(t['macd_hist_last']):.3f}"
        rsi = "-" if t.get("rsi_last") is None else f"{float(t['rsi_last']):.1f}"
        kdj_j = "-" if t.get("kdj_j_last") is None else f"{float(t['kdj_j_last']):.1f}"
        direction = t.get("trend_direction", "flat") or "flat"
        strength = t.get("trend_strength", 0.0) or 0.0
        trend = f"{direction}({float(strength)*100:.1f}%)"
        sups = ",".join(f"{float(x):.2f}" for x in (t.get("support_levels") or [])[:3])
        ress = ",".join(f"{float(x):.2f}" for x in (t.get("resistance_levels") or [])[:3])
        sigs = "、".join(t.get("signals") or [])
        lines.append(
            "| {c} | {ma} | {h} | {r} | {j} | {td} | {sup} | {res} | {sig} |".format(
                c=_md_escape(t.get("code", "")),
                ma=_md_escape(ma_str),
                h=_md_escape(hist),
                r=_md_escape(rsi),
                j=_md_escape(kdj_j),
                td=_md_escape(trend),
                sup=_md_escape(sups) or "-",
                res=_md_escape(ress) or "-",
                sig=_md_escape(sigs[:40] + ("…" if len(sigs) > 40 else "")),
            )
        )
    lines.append("")
    return lines


def _render_limit_section(limit_summary: dict[str, Any]) -> list[str]:
    lines: list[str] = ["## 1. 涨跌停汇总", ""]
    total = int(limit_summary.get("total_count") or 0)
    up = int(limit_summary.get("up_count") or 0)
    down = int(limit_summary.get("down_count") or 0)
    flat = int(limit_summary.get("flat_count") or 0)
    lines.append(f"- 统计样本：**{total}**")
    lines.append(f"- 涨停：**{up}**（{_format_pct(limit_summary.get('up_ratio'))}）")
    lines.append(f"- 跌停：**{down}**（{_format_pct(limit_summary.get('down_ratio'))}）")
    lines.append(f"- 其余：**{flat}**")
    lines.append("")
    by_type = limit_summary.get("by_type") or []
    if by_type:
        lines.append("### 按类型分布")
        lines.append("")
        for item in by_type:
            lines.append(f"- {item.get('limit_type','?')}：{int(item.get('count',0))}")
        lines.append("")
    by_sector = limit_summary.get("by_sector") or []
    if by_sector:
        lines.append("### 按行业分布（涨停+跌停 Top 10）")
        lines.append("")
        head = "| 行业 | 涨停 | 跌停 | 代表代码 |"
        sep = "|---|---:|---:|---|"
        lines.append(head)
        lines.append(sep)
        for s in by_sector[:10]:
            lines.append(
                "| {s} | {u} | {d} | {rc} |".format(
                    s=_md_escape(s.get("sector", "")),
                    u=int(s.get("up") or 0),
                    d=int(s.get("down") or 0),
                    rc=_md_escape(",".join(s.get("representative_codes") or [])),
                )
            )
        lines.append("")
    return lines


def _render_highlights(highlights: list[dict[str, Any]], signals_today: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = ["## 2. 技术亮点 & 今日信号", ""]
    if not highlights:
        lines.append("_暂无技术亮点。_")
    else:
        lines.append(f"技术亮点（信号数优先）：**{len(highlights)}** 只；")
        lines.append("")
        head = "| 代码 | 名称 | 信号数 | 趋势 | BOLL(上/中/下) | 支撑/压力 | 信号列表 |"
        sep = "|---|---|---:|---|---|---|---|"
        lines.append(head)
        lines.append(sep)
        for t in highlights[:20]:
            boll_parts = [t.get("boll_upper_last"), t.get("boll_mid_last"), t.get("boll_lower_last")]
            boll_str = "/".join("-" if v is None else f"{float(v):.2f}" for v in boll_parts)
            sr_parts = [
                ",".join(f"{float(x):.2f}" for x in (t.get("support_levels") or [])[:2]),
                ",".join(f"{float(x):.2f}" for x in (t.get("resistance_levels") or [])[:2]),
            ]
            sr = " / ".join(_md_escape(p) or "-" for p in sr_parts)
            sigs = "、".join(t.get("signals") or [])
            lines.append(
                "| {c} | {nm} | {n} | {tr} | {bl} | {sr} | {sg} |".format(
                    c=_md_escape(t.get("code", "")),
                    nm=_md_escape(t.get("name") or "-"),
                    n=len(t.get("signals") or []),
                    tr=_md_escape(f"{t.get('trend_direction','?')}({float(t.get('trend_strength') or 0.0)*100:.1f}%)"),
                    bl=_md_escape(boll_str),
                    sr=_md_escape(sr),
                    sg=_md_escape(sigs[:60] + ("…" if len(sigs) > 60 else "")),
                )
            )
        lines.append("")
    if signals_today:
        lines.append(f"今日新增信号：**{len(signals_today)}** 条；")
        lines.append("")
        for s in signals_today[:50]:
            nm = s.get("name") or ""
            label = f"{s.get('code')}" + (f"（{_md_escape(nm)}）" if nm else "")
            lines.append(
                "- **{c}**（{d}）：{sigs}".format(
                    c=label,
                    d=s.get("date"),
                    sigs="、".join(s.get("signals") or []),
                )
            )
        lines.append("")
    return lines


def _render_pnl(pnl_rows: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = ["## 3. 自选浮动盈亏", ""]
    if not pnl_rows:
        lines.append("_暂无自选持仓浮盈数据。_")
        lines.append("")
        return lines
    head = "| 代码 | 名称 | 昨收 | 今收 | 涨跌幅 | 持仓(股) | 估算浮盈 |"
    sep = "|---|---|---:|---:|---:|---:|---:|"
    lines.append(head)
    lines.append(sep)
    for p in pnl_rows[:30]:
        lines.append(
            "| {c} | {n} | {pc:.2f} | {c2:.2f} | {pct} | {qty} | {pnl:+.2f} |".format(
                c=_md_escape(p.get("code", "")),
                n=_md_escape(p.get("name", "")),
                pc=float(p.get("prev_close") or 0.0),
                c2=float(p.get("close") or 0.0),
                pct=_format_pct(p.get("change_pct")),
                qty=int(p.get("position_qty") or 0),
                pnl=float(p.get("est_pnl") or 0.0),
            )
        )
    lines.append("")
    return lines


def report_markdown(
    report: C.PreOpenReport | C.PostCloseReport,
) -> str:
    """盘前 / 盘后报告 → Markdown 文本。"""
    if isinstance(report, C.PreOpenReport):
        return _pre_markdown(report)
    if isinstance(report, C.PostCloseReport):
        return _post_markdown(report)
    raise DataIntegrityError(
        "[fail-loud/NF-26] report_markdown 仅接受 PreOpenReport / PostCloseReport，"
        f"实际类型 {type(report).__name__}"
    )


def _pre_markdown(report: C.PreOpenReport) -> str:
    lines: list[str] = []
    lines.append(f"# 盘前分析报告 · {report.date.isoformat()}（{report.market}）")
    lines.append("")
    lines.append(f"> 生成时间：{report.generated_at.isoformat(timespec='seconds')}")
    lines.append("")
    lines.extend(_render_news_section(dict(report.news_feed_summary)))
    lines.extend(_render_fundamental_section(list(report.watchlist_profiles)))
    lines.extend(_render_tech_section(list(report.broad_market_scan_top)))
    lines.append("---")
    lines.append("_本报告由 Kuantix 自动生成，仅用于量化研究，不构成投资建议。_")
    text = "\n".join(lines)
    if len(text) < 500:
        # 保证长度，避免空壳
        tail = (
            "\n\n**附**：本次盘前扫描 watchlist_profiles="
            f"{len(report.watchlist_profiles)}，broad_market_scan_top="
            f"{len(report.broad_market_scan_top)}。若需完整截面请通过 "
            "`GET /api/v1/analysis/pre-open/news|fundamentals` 分页查询。\n"
        )
        text += tail
    return text


def _post_markdown(report: C.PostCloseReport) -> str:
    lines: list[str] = []
    lines.append(f"# 盘后复盘报告 · {report.date.isoformat()}（{report.market}）")
    lines.append("")
    lines.append(f"> 生成时间：{report.generated_at.isoformat(timespec='seconds')}")
    lines.append("")
    lines.extend(_render_limit_section(dict(report.limit_summary)))
    lines.extend(
        _render_highlights(
            list(report.tech_highlights),
            list(report.signals_today),
        )
    )
    lines.extend(_render_pnl(list(report.watchlist_pnl)))
    lines.append("---")
    lines.append("_本报告由 Kuantix 自动生成，仅用于量化研究，不构成投资建议。_")
    text = "\n".join(lines)
    if len(text) < 500:
        text += (
            "\n\n**附**：tech_highlights="
            f"{len(report.tech_highlights)}；signals_today="
            f"{len(report.signals_today)}；watchlist_pnl="
            f"{len(report.watchlist_pnl)}。完整条目请通过 "
            "`GET /api/v1/analysis/post-close/limit-up-down|technical` 分页查询。\n"
        )
    return text
