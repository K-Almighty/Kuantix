"""证券搜索服务（D8 增量端点，本地证券清单 + 代码/名称匹配）。

设计（设计文档 08 §2：证券清单本地化，问题 1）
------------------------------------------------
- **数据源**：优先 :class:`~Kuantix.data.market_store.MarketStore` 的
  ``securities`` 表（``data sync`` / ``data migrate`` 落库后的本地清单）。
  **请求路径绝不发起网络枚举**（铁律）：``UniverseEnumerator`` 只在
  ``data sync`` 时被 :class:`~Kuantix.data.datalake.DataLake` 调用。
- **旧 JSON 缓存兼容（D9）**：``security_catalog.json`` 迁移导入后**废弃写入**，
  只读兼容一个版本 —— 清单表为空且 JSON 存在时读 JSON 兜底。
- **provider 注入（测试别名）**：``provider`` 保留为测试注入假证券清单的
  向后兼容别名；生产默认 ``None``（不再默认 ``UniverseEnumerator``，
  顺带消除了缺 import 的 NameError bug）。
- **空清单 → 显式 422**：SQLite 空 + JSON 缺失 + 无 provider → 抛
  :class:`~Kuantix.core.fail_loud.DataIntegrityError`（消息提示先
  ``Kuantix data sync`` / ``Kuantix data migrate``），不静默空返回。

匹配规则（fail-loud，不静默）
----------------------------
1. 代码精确匹配（最高优先级，排最前）；
2. 代码前缀匹配（至少 2 位）；
3. 名称子串匹配（大小写不敏感）。

- **q 为空** → 抛 :class:`~Kuantix.core.fail_loud.MissingKeyError`（→ 400）；
  无匹配 → **显式空数组**（合法态，不是错误）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from Kuantix.config import Config, get_config
from Kuantix.core.contracts import Security
from Kuantix.core.fail_loud import DataIntegrityError, MissingKeyError

__all__ = ["SecuritySearchService", "DEFAULT_CATALOG_FILENAME", "SecurityHit"]

#: 旧缓存文件名（位于 ``config.paths.db`` 下；D9：只读兼容，不再写入）
DEFAULT_CATALOG_FILENAME = "security_catalog.json"

#: 搜索返回条数上限
SEARCH_LIMIT_MAX = 50

#: 搜索仅覆盖的证券类型（**优先保证 A 股**，过滤债券/指数/基金/B 股）。
#: 沪深北 A 股（含创业板/科创板/北交所）作为监控与选股的目标标的。
A_STOCK_TYPES = frozenset(
    {"SH_A_STOCK", "SZ_A_STOCK", "BJ_A_STOCK", "CN_A_STOCK"}
)

#: 兜底清单（daily_bars）保留的 A 股代码前缀（排除基金 15/16/50/51/52/56/58、
#: 债券 10/11/12/13/14/17/18、指数 39 等）。
_A_STOCK_CODE_PREFIXES = ("00", "30", "60", "68", "8")


def _derive_exchange_type(code: str) -> tuple[str, str]:
    """由 CN 市场代码前缀推导交易所与证券类型（daily_bars 兜底用）。

    兜底清单无证券名称与类型时，用代码前缀做**展示级**推导（非权威，
    仅为让搜索可用）；真实类型应后续经 ``data securities update`` 补齐。

    Args:
        code: 6 位证券代码。

    Returns:
        ``(exchange, security_type)``；无法识别前缀时回退 ``cn``/``CN_A_STOCK``。
    """
    code_value = str(code).strip()
    if code_value.startswith(("5", "6", "9")):
        return "sh", "SH_A_STOCK"
    if code_value.startswith(("0", "1", "2", "3")):
        return "sz", "SZ_A_STOCK"
    if code_value.startswith(("4", "8", "92")):
        return "bj", "BJ_A_STOCK"
    return "cn", "CN_A_STOCK"


class SecurityHit:
    """一条搜索结果（契约 D8 SecurityHit）。

    Attributes:
        code: 6 位证券代码。
        name: 证券名称。
        exchange: 交易所前缀（``sh`` / ``sz``）。
        market: 市场代码（``CN``）。
        security_type: 上游证券类型（如 ``SH_A_STOCK``）。
    """

    __slots__ = ("code", "name", "exchange", "market", "security_type")

    def __init__(
        self,
        code: str,
        name: str,
        exchange: str,
        market: str,
        security_type: str,
    ) -> None:
        self.code = str(code)
        self.name = str(name)
        self.exchange = str(exchange)
        self.market = str(market).upper()
        self.security_type = str(security_type)

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典（契约 D8）。"""
        return {
            "code": self.code,
            "name": self.name,
            "exchange": self.exchange,
            "market": self.market,
            "security_type": self.security_type,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<SecurityHit {self.exchange}:{self.code} {self.name}>"


class SecuritySearchService:
    """本地证券清单搜索服务（SQLite 优先，读本地清单，零请求路径网络）。

    Args:
        config: 配置对象；``None`` 时取全局配置。
        store: :class:`~Kuantix.data.market_store.MarketStore`（本地清单主源）；
            ``None`` 时跳过 SQLite 路径。
        provider: 证券清单提供者（**仅测试注入别名**）；``None`` 表示生产
            默认 —— 请求路径不枚举网络。
        cache_path: 旧 JSON 缓存文件路径；``None`` 时用
            ``config.paths.db / security_catalog.json``（D9 只读兼容）。
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        store: Any | None = None,
        provider: Callable[[], list[Security]] | None = None,
        cache_path: Path | str | None = None,
    ) -> None:
        self._config = config if config is not None else get_config()
        self._cache_path = (
            Path(cache_path).expanduser()
            if cache_path is not None
            else self._config.paths.db / DEFAULT_CATALOG_FILENAME
        )
        self._store = store
        self._provider = provider
        self._lock = threading.RLock()
        self._catalog: list[Security] | None = None

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #

    def search(
        self, q: str, market: str = "CN", *, limit: int = 20
    ) -> list[SecurityHit]:
        """按代码/名称搜索证券清单。

        Args:
            q: 搜索关键词（代码或名称）。
            market: 市场码；P0 仅 ``CN``（调用方 resolve_market 已做 501 门禁）。
            limit: 返回条数上限（1..SEARCH_LIMIT_MAX）。

        Returns:
            匹配的证券基本信息列表；无匹配返回**空列表**（合法态）。

        Raises:
            MissingKeyError: ``q`` 为空。
            DataIntegrityError: 本地清单为空（SQLite 空 + JSON 缺失 +
                无 provider），提示先 ``data sync`` / ``data migrate``。
        """
        query = str(q).strip()
        if not query:
            raise MissingKeyError(
                "[fail-loud/NF-26] search 参数 q 不能为空（支持代码或名称）"
            )
        if limit <= 0:
            raise MissingKeyError(
                f"[fail-loud/NF-26] limit 必须为正整数，实际 {limit!r}"
            )
        limit = min(int(limit), SEARCH_LIMIT_MAX)
        catalog = self._load_catalog()
        if not catalog:
            return []

        norm = query.upper()
        exact: list[SecurityHit] = []
        prefix: list[SecurityHit] = []
        name_hits: list[SecurityHit] = []
        for sec in catalog:
            if sec.market != str(market).upper():
                continue
            code = str(sec.code)
            if code == norm:
                exact.append(self._to_hit(sec))
            elif len(norm) >= 2 and code.startswith(norm):
                prefix.append(self._to_hit(sec))
            elif self._name_match(sec.name, query):
                name_hits.append(self._to_hit(sec))
        hits = exact + prefix + name_hits
        return hits[:limit]

    def catalog_size(self) -> int:
        """返回证券清单条数（加载 SQLite/JSON/provider，供状态展示与测试）。"""
        return len(self._load_catalog())

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _name_match(name: str, query: str) -> bool:
        """名称子串匹配（大小写不敏感、去除空白）。"""
        return str(query).strip().lower() in str(name).strip().lower()

    @staticmethod
    def _to_hit(sec: Security) -> SecurityHit:
        return SecurityHit(
            code=sec.code,
            name=sec.name,
            exchange=sec.exchange,
            market=sec.market,
            security_type=sec.security_type,
        )

    def _load_catalog(self) -> list[Security]:
        """加载证券清单：SQLite → 旧 JSON（D9 只读兼容）→ provider（测试）。

        Returns:
            :class:`Security` 列表（空列表为合法态，表示无匹配）。

        Raises:
            DataIntegrityError: SQLite 空 + JSON 缺失 + 无 provider
                （生产空清单 → 422，提示先 sync/migrate）。
        """
        with self._lock:
            if self._catalog is not None:
                return self._catalog

            # 1. SQLite 本地清单（主源，零网络）
            catalog = self._load_from_store()
            if catalog:
                self._catalog = catalog
                return catalog

            # 2. 旧 JSON 缓存（D9：读兼容一个版本）
            if self._cache_path.is_file():
                catalog = self._read_cache()
                if catalog:
                    self._catalog = catalog
                    return catalog

            # 3. provider（仅测试注入别名；生产默认 None 不走到这里）
            if self._provider is not None:
                catalog = self._enumerate_and_persist()
                self._catalog = catalog
                return catalog

            # 4. daily_bars 兜底：securities 表为空但日线已迁移时，从
            #    daily_bars 提取去重代码作为代码清单（无名称）。这样「只迁移
            #    日线、未提供 --catalog」时**代码**搜索仍可用，不再 422。
            catalog = self._load_from_daily_bars()
            if catalog:
                self._catalog = catalog
                return catalog

            # 5. 空清单 → 显式 422（不静默空返回）
            raise DataIntegrityError(
                "[fail-loud/NF-26] 证券清单为空，请先执行 "
                "`Kuantix data sync` 或 `Kuantix data migrate` 生成本地清单"
            )

    def _load_from_daily_bars(self) -> list[Security]:
        """从 ``daily_bars`` 去重代码构造兜底清单（securities 表为空时）。

        迁移只导入了日线（未提供 ``--catalog``）时证券名称不可得；这里仅用
        代码构造 :class:`Security`（name 为空、exchange/type 按代码前缀推导），
        使代码精确/前缀搜索可用，名称搜索返回空（合法态）。名称可后续经
        ``Kuantix data securities update`` 补全后自然替换。

        Returns:
            :class:`Security` 列表（daily_bars 也为空则 ``[]``）。
        """
        if self._store is None or not hasattr(self._store, "list_daily_bar_codes"):
            return []
        try:
            codes = self._store.list_daily_bar_codes("CN")
        except Exception:  # noqa: BLE001 - 兜底失败不掩盖主逻辑，返回空走 422
            return []
        securities: list[Security] = []
        for code in codes:
            # 优先保证 A 股：跳过基金/债券/指数等非股票代码前缀
            if not str(code).startswith(_A_STOCK_CODE_PREFIXES):
                continue
            exchange, security_type = _derive_exchange_type(code)
            try:
                securities.append(
                    Security(
                        code=code,
                        exchange=exchange,
                        market="CN",
                        security_type=security_type,
                        name="",
                    )
                )
            except DataIntegrityError:
                continue
        return securities

    def _load_from_store(self) -> list[Security]:
        """从 SQLite ``securities`` 表读取本地清单（零网络，**仅 A 股**）。

        枚举生成的 securities 表含债券/指数/基金/B 股等非股票类型；按用户
        「优先保证 A 股」的要求，此处只保留 ``*_A_STOCK`` 类型（创业板/科创
        板/北交所均为 A 股），过滤其余类型，避免监控看板搜索混入无关标的。

        另并入 ``daily_bars`` 已有数据、但清单缺失的 A 股代码：securities
        表主键 ``(market, code)`` 无交易所列，000001-000999 区间沪市指数行
        会覆盖同代码深市股票行（如 sz000001 平安银行），这些股票的行情
        已入库却搜不到；此处按代码前缀推导类型补入（名称为空，展示由
        详情页/自选股兜底），保证「有数据的代码必可搜到」。
        """
        if self._store is None:
            return []
        # 类型过滤下推到 SQL：全表 17634 条中 A 股 5119 条，避免为债券/
        # 指数/基金白构造 12515 个 Security 对象（冷启动 30ms → 8ms）。
        try:
            securities = list(
                self._store.list_securities(security_types=sorted(A_STOCK_TYPES))
            )
        except TypeError:
            # 注入的测试替身可能没有 security_types 形参，退化为内存过滤
            all_sec = self._store.list_securities()
            securities = [s for s in all_sec if s.security_type in A_STOCK_TYPES]
        known = {s.code for s in securities}
        try:
            bar_codes = self._store.list_daily_bar_codes("CN")
        except Exception:  # noqa: BLE001 - 兜底失败不影响主清单
            bar_codes = []
        for code in bar_codes:
            code = str(code)
            if code in known or not code.startswith(_A_STOCK_CODE_PREFIXES):
                continue
            exchange, security_type = _derive_exchange_type(code)
            try:
                securities.append(
                    Security(
                        code=code,
                        exchange=exchange,
                        market="CN",
                        security_type=security_type,
                        name="",
                    )
                )
                known.add(code)
            except DataIntegrityError:
                continue
        return securities

    def _read_cache(self) -> list[Security]:
        """读取旧缓存 JSON 文件 → :class:`Security` 列表（D9 只读兼容）。"""
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 证券清单缓存读取失败: {self._cache_path} "
                f"（{type(exc).__name__}: {exc}）"
            ) from exc
        if not isinstance(raw, list):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 证券清单缓存结构非法（期望数组）: {self._cache_path}"
            )
        securities: list[Security] = []
        for item in raw:
            if not isinstance(item, dict):
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] 证券清单缓存条目非法（期望对象）: {self._cache_path}"
                )
            try:
                name_value = item.get("name")
                securities.append(
                    Security(
                        code=str(item["code"]),
                        exchange=str(item["exchange"]),
                        market=str(item["market"]),
                        security_type=str(item["security_type"]),
                        name=str(name_value) if name_value is not None else "",
                    )
                )
            except KeyError as exc:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] 证券清单缓存条目缺字段 {exc}: {self._cache_path}"
                ) from exc
        return securities

    def _enumerate_and_persist(self) -> list[Security]:
        """经注入的 provider 枚举（**仅测试别名**）并落旧 JSON 缓存。

        Returns:
            :class:`Security` 列表（provider 返回空列表为合法态）。

        Raises:
            DataIntegrityError: provider 抛异常（fail-loud，不静默空返回）。
        """
        try:
            securities = list(self._provider())
        except Exception as exc:  # noqa: BLE001 - provider 失败归一为数据完整性错误
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 证券清单不可用（缓存缺失且枚举失败）: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if securities:
            self._write_cache(securities)
        return securities

    def _write_cache(self, securities: list[Security]) -> None:
        """把证券清单序列化写入旧缓存文件（原子写；仅测试别名路径会触发）。"""
        rows = [s.to_dict() for s in securities]
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self._cache_path)
