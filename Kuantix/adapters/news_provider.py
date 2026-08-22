"""消息面 Provider 抽象（NF-24/R2：外部访问统一收敛到 adapters 层）。

对外仅暴露：
- :class:`NewsProvider`（Protocol 抽象，供业务层依赖）
- :func:`create_news_provider`（基于配置构造，fail-loud）
- :class:`LocalJsonNewsProvider`（参考实现：从目录读取 ``$date.json``）

后续若接入巨潮 / 东方财富 / Tushare 等真实源，新增一个实现类 + 在
``create_news_provider`` 注册即可；业务层（PreOpenService）**无需改动**。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

from Kuantix.config import Config
from Kuantix.core import contracts as C
from Kuantix.core.fail_loud import DataIntegrityError, NotSupportedError

__all__ = ["NewsProvider", "LocalJsonNewsProvider", "create_news_provider"]


@runtime_checkable
class NewsProvider(Protocol):
    """消息面提供方协议（鸭子类型，供 DI / 测试注入）。"""

    def fetch(
        self,
        market: str,
        date: dt.date,
        keywords: Iterable[str] | None = None,
    ) -> list[C.NewsItem]:
        """返回指定交易日的消息面条目（按 importance DESC 已排序）。"""
        ...


@dataclass
class LocalJsonNewsProvider:
    """本地 JSON 示例 Provider：目录下每个交易日一份 ``YYYY-MM-DD.json``。

    目录结构：
        <base_dir>/
            2026-01-05.json
            2026-01-06.json
            ...

    每条 JSON 记录::

        {
          "id": "unique",
          "source": "巨潮",
          "category": "announcement", // news | announcement | policy
          "title": "...",
          "url": "https://...",
          "publish_ts": "2026-01-05T08:10:00+08:00",
          "codes": ["600519", "000001"],
          "importance": 8,
          "matched_keywords": ["业绩预增"],
          "summary": "..."
        }
    """

    base_dir: Path

    def fetch(
        self,
        market: str,
        date: dt.date,
        keywords: Iterable[str] | None = None,
    ) -> list[C.NewsItem]:
        base = Path(self.base_dir).expanduser()
        data_file = base / f"{date.isoformat()}.json"
        if not data_file.is_file():
            return []
        try:
            raw_list: list[dict[str, Any]] = json.loads(data_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 本地消息样本 JSON 解析失败: {data_file} ({exc})"
            ) from exc
        if not isinstance(raw_list, list):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 本地消息样本顶层必须是数组，实际 {type(raw_list).__name__}"
            )
        kw_set = {str(k).strip().lower() for k in (keywords or []) if str(k).strip()}
        result: list[C.NewsItem] = []
        for idx, item in enumerate(raw_list):
            if not isinstance(item, dict):
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] 样本 {data_file} 第 {idx} 条不是对象"
                )
            matched = _kw_match(item, kw_set)
            if kw_set and not matched:
                continue
            codes = item.get("codes", [])
            keywords_list = item.get("matched_keywords", [])
            try:
                publish_ts = dt.datetime.fromisoformat(str(item["publish_ts"]))
            except (ValueError, KeyError) as exc:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] 样本 publish_ts 非法: {item.get('publish_ts')!r} ({exc})"
                ) from exc
            entry = C.NewsItem(
                id=str(item.get("id") or f"{date.isoformat()}-{idx}"),
                source=str(item.get("source") or "local_json"),
                category=item.get("category", C.NewsCategory.NEWS.value),
                title=str(item.get("title") or "(无标题)"),
                url=str(item.get("url") or ""),
                publish_ts=publish_ts,
                codes=tuple(str(c) for c in (codes if isinstance(codes, list) else [])),
                importance=int(item.get("importance", 0)),
                matched_keywords=tuple(str(k) for k in (keywords_list if isinstance(keywords_list, list) else [])),
                summary=str(item.get("summary") or ""),
            )
            result.append(entry)
        result.sort(key=lambda x: (-int(x.importance), x.publish_ts))
        return result


def _kw_match(item: dict[str, Any], kw_set: set[str]) -> bool:
    if not kw_set:
        return True
    pools: list[str] = []
    for key in ("title", "summary"):
        value = item.get(key)
        if isinstance(value, str):
            pools.append(value.lower())
    keywords_in_payload = item.get("matched_keywords")
    if isinstance(keywords_in_payload, list):
        pools.append(" ".join(str(k).lower() for k in keywords_in_payload))
    text = "\n".join(pools)
    return any(k in text for k in kw_set)


def create_news_provider(name: str, config: Config) -> NewsProvider:
    """按配置构造 NewsProvider。未知 provider → NotSupportedError（fail-loud）。"""
    selected = str(name).strip().lower()
    if not selected:
        raise NotSupportedError(
            "[fail-loud/NF-26] news_provider 名称不能为空"
        )
    if selected == "local_json":
        path = config.analysis.news_path
        if path is None:
            raise NotSupportedError(
                "[fail-loud/NF-26] LocalJsonNewsProvider 需要配置 analysis.news_path"
            )
        return LocalJsonNewsProvider(base_dir=Path(path))
    raise NotSupportedError(
        f"[fail-loud/NF-26] 未知 news_provider={name!r}；"
        f"当前仅实现 local_json。请实现自定义 Provider 后在此处注册。"
    )
