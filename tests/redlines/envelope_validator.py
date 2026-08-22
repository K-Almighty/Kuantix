"""JSON 信封契约校验器（NF-9 / NF-12）。

供后续所有 CLI / REST 验收用例复用。核心接口只有一个：

    validate_envelope(obj) -> list[str]

返回违规描述列表；**空列表表示通过**。不抛异常、不打印，调用方自行决定
是 assert 还是记录——这样既能在单测里 ``assert not validate_envelope(r)``，
也能在批量验收里聚合统计。

约束来源
--------
- **NF-9 统一信封**：所有 JSON 响应结构一致
  ``{code, message, data, meta}``；``meta`` 含
  ``generated_at / data_date / market / elapsed_ms / version``。
- **NF-6 市场标识贯穿全链路**：``meta.market`` 必填。
- **NF-12 数值安全**：JSON 中禁止出现 ``NaN`` / ``Infinity`` / ``-Infinity``
  （非法 JSON），统一序列化为 ``null``；浮点保留 6 位小数。
- **NF-10 双入口对等**：CLI ``--json`` 与 REST 返回同一份 schema
  —— 所以本校验器对两个入口通用。

用法
----
    from envelope_validator import validate_envelope, validate_envelope_json

    # 1) 校验已解析的 dict
    problems = validate_envelope(json.loads(stdout))
    assert not problems, "\\n".join(problems)

    # 2) 校验原始 JSON 文本（能额外抓到裸 NaN/Infinity 字面量）
    problems = validate_envelope_json(stdout)
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

__all__ = [
    "ENVELOPE_KEYS",
    "META_KEYS",
    "FLOAT_PRECISION",
    "validate_envelope",
    "validate_envelope_json",
    "find_non_finite",
    "find_overprecise_floats",
    "assert_envelope",
]

#: NF-9 顶层四键
ENVELOPE_KEYS: tuple[str, ...] = ("code", "message", "data", "meta")

#: NF-9 meta 必填键
META_KEYS: tuple[str, ...] = (
    "generated_at",
    "data_date",
    "market",
    "elapsed_ms",
    "version",
)

#: NF-12 浮点保留位数
FLOAT_PRECISION = 6

#: JSON 文本里的非法数值字面量（Python json 默认会把它们写成裸 token）
_NON_FINITE_TOKEN_RE = re.compile(r"(?<![\w\"])(-?Infinity|NaN)(?![\w\"])")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------
# 路径工具
# --------------------------------------------------------------------------


def _child(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}"


def _walk(node: Any, path: str = "$"):
    """深度优先遍历整棵 JSON 树，产出 (路径, 值)。"""
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, _child(path, k))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _walk(v, _child(path, i))


# --------------------------------------------------------------------------
# NF-12：非有限浮点 / 超精度浮点
# --------------------------------------------------------------------------


def find_non_finite(obj: Any) -> list[str]:
    """全树递归查找 NaN / Infinity / -Infinity（NF-12）。"""
    problems: list[str] = []
    for path, val in _walk(obj):
        if isinstance(val, float) and not math.isfinite(val):
            kind = "NaN" if math.isnan(val) else ("Infinity" if val > 0 else "-Infinity")
            problems.append(
                f"[NF-12] {path} = {kind}：JSON 不允许非有限数值，必须序列化为 null"
            )
        elif isinstance(val, str) and val in ("NaN", "Infinity", "-Infinity", "inf", "-inf", "nan"):
            problems.append(
                f'[NF-12] {path} = "{val}"：把非有限数值转成字符串同样违规，必须是 null'
            )
    return problems


def _decimal_places(x: float) -> int:
    """用 repr 判定小数位数，避免二进制浮点误差造成误判。"""
    s = repr(float(x))
    if "e" in s or "E" in s:
        # 科学计数法（如 1e-09）：按指数判定
        mantissa, _, exp = s.partition("e")
        frac = len(mantissa.partition(".")[2])
        return max(0, frac - int(exp))
    return len(s.partition(".")[2].rstrip("0"))


def find_overprecise_floats(obj: Any, precision: int = FLOAT_PRECISION) -> list[str]:
    """全树递归查找小数位数超过 ``precision`` 的浮点（NF-12）。"""
    problems: list[str] = []
    for path, val in _walk(obj):
        if isinstance(val, float) and math.isfinite(val):
            places = _decimal_places(val)
            if places > precision:
                problems.append(
                    f"[NF-12] {path} = {val!r} 小数位 {places} 超过 {precision} 位："
                    f"输出前需 round(x, {precision})"
                )
    return problems


# --------------------------------------------------------------------------
# NF-9：信封结构
# --------------------------------------------------------------------------


def _validate_meta(meta: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(meta, dict):
        return [f"[NF-9] $.meta 必须是对象，实际为 {type(meta).__name__}"]

    for key in META_KEYS:
        if key not in meta:
            problems.append(f"[NF-9] $.meta 缺少必填字段 `{key}`")

    if "market" in meta:
        market = meta["market"]
        if not isinstance(market, str) or not market.strip():
            problems.append(
                f"[NF-6] $.meta.market 必须是非空字符串（市场标识贯穿全链路），实际为 {market!r}"
            )

    if "elapsed_ms" in meta:
        elapsed = meta["elapsed_ms"]
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            problems.append(f"[NF-9] $.meta.elapsed_ms 必须是数值，实际为 {elapsed!r}")
        elif elapsed < 0:
            problems.append(f"[NF-9] $.meta.elapsed_ms 不能为负，实际为 {elapsed!r}")

    if "version" in meta and not isinstance(meta["version"], str):
        problems.append(f"[NF-9] $.meta.version 必须是字符串，实际为 {meta['version']!r}")

    if "generated_at" in meta:
        ts = meta["generated_at"]
        if not isinstance(ts, str):
            problems.append(f"[NF-9] $.meta.generated_at 必须是 ISO8601 字符串，实际为 {ts!r}")
        else:
            try:
                datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                problems.append(
                    f'[NF-9] $.meta.generated_at "{ts}" 不是合法 ISO8601 时间戳'
                )

    if "data_date" in meta:
        d = meta["data_date"]
        if d is not None and not (isinstance(d, str) and _ISO_DATE_RE.match(d)):
            problems.append(
                f'[NF-9] $.meta.data_date 应为 "YYYY-MM-DD" 或 null，实际为 {d!r}'
            )

    return problems


def validate_envelope(
    obj: Any,
    *,
    precision: int = FLOAT_PRECISION,
    allow_extra_top_keys: bool = False,
) -> list[str]:
    """校验统一 JSON 信封（NF-9 + NF-12 + NF-6）。

    Args:
        obj: 已解析的 JSON 对象（dict）。也接受 JSON 文本，会先行解析。
        precision: 浮点最大小数位数，默认 6（NF-12）。
        allow_extra_top_keys: 是否容忍顶层出现四键之外的字段，默认 False
            —— NF-9 要求「所有 JSON 响应结构一致」，多出来的键会破坏
            Agent 侧的稳定解析。

    Returns:
        违规描述列表；空列表表示完全符合契约。
    """
    problems: list[str] = []

    if isinstance(obj, (str, bytes)):
        return validate_envelope_json(
            obj if isinstance(obj, str) else obj.decode("utf-8"),
            precision=precision,
            allow_extra_top_keys=allow_extra_top_keys,
        )

    if not isinstance(obj, dict):
        return [f"[NF-9] 顶层必须是对象 {{code, message, data, meta}}，实际为 {type(obj).__name__}"]

    # --- 顶层四键 ---------------------------------------------------------
    for key in ENVELOPE_KEYS:
        if key not in obj:
            problems.append(f"[NF-9] 顶层缺少必填字段 `{key}`")

    if not allow_extra_top_keys:
        extra = [k for k in obj if k not in ENVELOPE_KEYS]
        if extra:
            problems.append(
                f"[NF-9] 顶层出现契约外字段 {sorted(extra)}："
                "统一信封固定为 {code, message, data, meta}，附加信息请放进 data 或 meta"
            )

    if "code" in obj and (isinstance(obj["code"], bool) or not isinstance(obj["code"], int)):
        problems.append(f"[NF-9] $.code 必须是整数，实际为 {obj['code']!r}")

    if "message" in obj and not isinstance(obj["message"], str):
        problems.append(f"[NF-9] $.message 必须是字符串，实际为 {obj['message']!r}")

    if "meta" in obj:
        problems.extend(_validate_meta(obj["meta"]))

    # --- NF-12 全树数值安全 ------------------------------------------------
    problems.extend(find_non_finite(obj))
    problems.extend(find_overprecise_floats(obj, precision))

    return problems


def _reject_constant(token: str):
    raise ValueError(
        f"[NF-12] 原始 JSON 文本中出现非法字面量 `{token}`："
        "JSON 规范不允许 NaN/Infinity/-Infinity，必须序列化为 null"
    )


def validate_envelope_json(
    text: str,
    *,
    precision: int = FLOAT_PRECISION,
    allow_extra_top_keys: bool = False,
) -> list[str]:
    """校验**原始 JSON 文本**。

    相比 :func:`validate_envelope`，本函数能额外抓到 Python ``json.dumps``
    默认写出的裸 ``NaN`` / ``Infinity`` token —— 这类输出连标准 JSON 解析器
    都读不了，是 NF-12 最典型的踩坑方式。
    """
    problems: list[str] = []

    tokens = sorted(set(_NON_FINITE_TOKEN_RE.findall(text)))
    if tokens:
        problems.append(
            f"[NF-12] 原始 JSON 文本含非法字面量 {tokens}："
            "标准 JSON 解析器无法读取，必须输出 null"
            "（Python 侧用 json.dumps(..., allow_nan=False) 可提前暴露）"
        )

    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        problems.append(f"[NF-9] JSON 解析失败：{exc}")
        return problems

    problems.extend(
        validate_envelope(obj, precision=precision, allow_extra_top_keys=allow_extra_top_keys)
    )
    return problems


def assert_envelope(obj: Any, **kwargs: Any) -> None:
    """便捷断言封装：不通过时抛 AssertionError 并列出全部违规。"""
    problems = validate_envelope(obj, **kwargs)
    if problems:
        raise AssertionError(
            "JSON 信封契约校验未通过（NF-9/NF-12），共 "
            f"{len(problems)} 项：\n" + "\n".join(f"  - {p}" for p in problems)
        )
