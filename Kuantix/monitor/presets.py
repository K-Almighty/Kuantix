"""监控看板 · 预设监控规则（插件式注册表，可扩展）。

设计目标
--------
- 提供一组"开箱即用"的常用监控条件（涨停 / 跌停 / 涨跌幅异常 / 成交量异常 /
  价格突破关键位），**默认开启**；
- 用户可在界面**一键关闭 / 启用**任一预设（通过 ``toggle_preset``）；
- 未来扩展：只需在 :data:`PRESETS` 注册一个新 :class:`PresetTemplate`，
  前端无需改动（预设列表由后端 ``GET /api/v1/monitor/presets`` 返回）。

每个预设对应一条注入到 ``rules`` 表的真实规则（``source="preset"``），
``preset_key`` 用于定位与一键切换。预设默认 ``enabled=True``。

判据依赖（见 ``rules.py``）：
- ``change_pct``：涨跌幅阈值（读 ``quote.change_pct``，无需历史）；
- ``volume``：成交额放量/缩量（读 ``quote.amount``，无需历史）；
- ``indicator``：技术指标（如价格上穿 MA20，复用现有插件）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from Kuantix.monitor.store import MonitorStore

logger = logging.getLogger(__name__)

#: 涨停/跌停近似阈值（普通 A 股 ±10%，取 ±9.5% 容差）
LIMIT_THRESHOLD = 0.095
#: 涨跌幅异常阈值（大幅异动）
CHANGE_ANOMALY_THRESHOLD = 0.05
#: 成交量异常成交额阈值（元）：单日成交额 > 5 亿元视为放量异动
VOLUME_ANOMALY_AMOUNT = 5e8
#: 默认冷却期（秒）：同一预设 5 分钟内不重复告警
DEFAULT_COOLDOWN = 300.0

MARKET = "CN"


@dataclass(frozen=True)
class PresetTemplate:
    """一个预设监控规则模板。

    Attributes:
        key: 唯一 key（对应 ``rules.preset_key``）。
        name: 展示名。
        description: 说明。
        criterion_type: 判据类型（``change_pct``/``volume``/``indicator``/``price``）。
        params: 判据参数。
        level: 默认告警级别（``info``/``warning``/``critical``）。
        cooldown_seconds: 默认冷却期（秒）。
        default_enabled: 是否默认开启（统一为 ``True``，满足"默认开启"需求）。
    """

    key: str
    name: str
    description: str
    criterion_type: str
    params: dict[str, Any]
    level: str
    cooldown_seconds: float = DEFAULT_COOLDOWN
    default_enabled: bool = True

    def build_rule_kwargs(self) -> dict[str, Any]:
        """生成注入 ``Manager.create_rule`` 所需的参数。"""
        return {
            "name": self.name,
            "market": MARKET,
            "codes": ("*",),
            "criterion_type": self.criterion_type,
            "params": dict(self.params),
            "level": self.level,
            "cooldown_seconds": self.cooldown_seconds,
            "enabled": self.default_enabled,
            "source": "preset",
            "preset_key": self.key,
        }


#: 预设注册表（新增规则：在此追加一项即可，前端自动渲染）。
PRESETS: dict[str, PresetTemplate] = {
    "limit_up": PresetTemplate(
        key="limit_up",
        name="股票涨停",
        description="当个股涨幅达到约 +9.5%（涨停板）时告警",
        criterion_type="change_pct",
        params={"op": "above", "threshold": LIMIT_THRESHOLD},
        level="warning",
    ),
    "limit_down": PresetTemplate(
        key="limit_down",
        name="股票跌停",
        description="当个股跌幅达到约 -9.5%（跌停板）时告警",
        criterion_type="change_pct",
        params={"op": "below", "threshold": -LIMIT_THRESHOLD},
        level="warning",
    ),
    "change_anomaly": PresetTemplate(
        key="change_anomaly",
        name="涨跌幅异常",
        description="当个股单日涨跌幅超过 ±5%（大幅异动）时告警",
        criterion_type="change_pct",
        params={"op": "above", "threshold": CHANGE_ANOMALY_THRESHOLD},
        level="info",
    ),
    "volume_anomaly": PresetTemplate(
        key="volume_anomaly",
        name="成交量异常",
        description="当个股单日成交额超过 5 亿元（放量异动）时告警",
        criterion_type="volume",
        params={"op": "above", "threshold": VOLUME_ANOMALY_AMOUNT},
        level="info",
    ),
    "key_level_break": PresetTemplate(
        key="key_level_break",
        name="价格突破关键位",
        description="当价格上穿 MA20 关键均线（突破关键位）时告警",
        criterion_type="indicator",
        params={"indicator": "ma", "op": "cross_above", "fast": 5, "slow": 20},
        level="info",
    ),
}


def list_preset_templates() -> list[PresetTemplate]:
    """返回全部预设模板（稳定顺序：注册表插入序）。"""
    return list(PRESETS.values())


def get_preset(key: str) -> PresetTemplate:
    """按 key 取预设模板（不存在显式报错，NF-26）。"""
    if key not in PRESETS:
        raise KeyError(f"[fail-loud/NF-26] 未知预设规则 key: {key}")
    return PRESETS[key]


def ensure_presets(store: MonitorStore) -> list[str]:
    """首次启动时注入全部预设为默认开启的规则（幂等）。

    已存在同 ``preset_key`` 的规则不再重复注入；仅对新部署生效。

    Returns:
        本次实际新注入的预设 key 列表。
    """
    injected: list[str] = []
    for tpl in list_preset_templates():
        existing = store.find_rule_by_preset_key(tpl.key)
        if existing is not None:
            continue
        rule_id = f"rule_preset_{tpl.key}"
        # 直接以最小契约形态写入 store（绕过 Manager 校验，避免重复判定据）
        store.add_rule(
            {
                "id": rule_id,
                "name": tpl.name,
                "market": MARKET,
                "codes": ["*"],
                "criterion_type": tpl.criterion_type,
                "params": dict(tpl.params),
                "level": tpl.level,
                "cooldown_seconds": tpl.cooldown_seconds,
                "enabled": tpl.default_enabled,
                "source": "preset",
                "preset_key": tpl.key,
                "created_at": __import__("datetime").datetime.now().astimezone(),
                "updated_at": __import__("datetime").datetime.now().astimezone(),
                "last_triggered_at": None,
            }
        )
        injected.append(tpl.key)
    if injected:
        logger.info("已注入预设监控规则: %s", injected)
    return injected
