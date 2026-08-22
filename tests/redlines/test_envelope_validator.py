"""``envelope_validator`` 自检用例（NF-9 / NF-12）。

这个校验器后续会被大量验收用例复用，所以它自己必须先被证明是对的：
既不能漏报（合法信封判违规 → 全组验收假失败），也不能误报。
本文件在源码尚未落地时就能全绿，作为契约层的"活文档"。
"""

from __future__ import annotations

import json
import math

import pytest

from envelope_validator import (
    ENVELOPE_KEYS,
    META_KEYS,
    assert_envelope,
    find_non_finite,
    find_overprecise_floats,
    validate_envelope,
    validate_envelope_json,
)


def make_envelope(**overrides):
    """构造一个符合 NF-9 的最小合法信封。"""
    env = {
        "code": 0,
        "message": "ok",
        "data": {"items": [{"code": "600000", "pe": 5.123456}], "total": 1},
        "meta": {
            "generated_at": "2026-08-01T21:30:00+08:00",
            "data_date": "2026-08-01",
            "market": "CN",
            "elapsed_ms": 123,
            "version": "0.1.0",
        },
    }
    env.update(overrides)
    return env


# --------------------------------------------------------------------------
# 正向：合法信封必须零违规
# --------------------------------------------------------------------------


def test_valid_envelope_passes():
    assert validate_envelope(make_envelope()) == []


def test_valid_envelope_json_text_passes():
    assert validate_envelope_json(json.dumps(make_envelope())) == []


def test_null_data_is_allowed():
    """NF-12 要求缺失值序列化为 null —— null 本身合法。"""
    env = make_envelope(data={"pe": None, "pb": None})
    assert validate_envelope(env) == []


def test_assert_envelope_silent_on_valid():
    assert_envelope(make_envelope())


# --------------------------------------------------------------------------
# NF-9：顶层结构
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ENVELOPE_KEYS)
def test_missing_top_level_key_detected(missing):
    env = make_envelope()
    env.pop(missing)
    problems = validate_envelope(env)
    assert any(f"顶层缺少必填字段 `{missing}`" in p for p in problems), problems


def test_extra_top_level_key_detected():
    env = make_envelope()
    env["extra"] = 1
    problems = validate_envelope(env)
    assert any("契约外字段" in p for p in problems), problems
    # 显式放宽时应放行
    assert validate_envelope(env, allow_extra_top_keys=True) == []


def test_non_dict_top_level_detected():
    problems = validate_envelope([1, 2, 3])
    assert problems and "顶层必须是对象" in problems[0]


def test_wrong_code_type_detected():
    problems = validate_envelope(make_envelope(code="0"))
    assert any("$.code 必须是整数" in p for p in problems), problems


def test_bool_code_rejected():
    """True 在 Python 里是 int 的子类，不能被当成合法 code。"""
    problems = validate_envelope(make_envelope(code=True))
    assert any("$.code 必须是整数" in p for p in problems), problems


# --------------------------------------------------------------------------
# NF-9：meta 必填字段
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing", META_KEYS)
def test_missing_meta_key_detected(missing):
    env = make_envelope()
    env["meta"].pop(missing)
    problems = validate_envelope(env)
    assert any(f"$.meta 缺少必填字段 `{missing}`" in p for p in problems), problems


def test_empty_market_detected():
    env = make_envelope()
    env["meta"]["market"] = ""
    problems = validate_envelope(env)
    assert any("$.meta.market" in p for p in problems), problems


def test_bad_generated_at_detected():
    env = make_envelope()
    env["meta"]["generated_at"] = "2026/08/01 21:30"
    problems = validate_envelope(env)
    assert any("generated_at" in p for p in problems), problems


def test_bad_data_date_detected():
    env = make_envelope()
    env["meta"]["data_date"] = "20260801"
    problems = validate_envelope(env)
    assert any("data_date" in p for p in problems), problems


def test_null_data_date_allowed():
    """尚未同步任何数据时 data_date 允许为 null。"""
    env = make_envelope()
    env["meta"]["data_date"] = None
    assert validate_envelope(env) == []


def test_negative_elapsed_ms_detected():
    env = make_envelope()
    env["meta"]["elapsed_ms"] = -1
    problems = validate_envelope(env)
    assert any("elapsed_ms" in p for p in problems), problems


# --------------------------------------------------------------------------
# NF-12：非有限数值
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf")], ids=["NaN", "Inf", "-Inf"]
)
def test_non_finite_in_nested_data_detected(bad):
    env = make_envelope()
    env["data"]["items"][0]["pe"] = bad
    problems = validate_envelope(env)
    assert any("NF-12" in p and "items[0].pe" in p for p in problems), problems


def test_non_finite_deeply_nested_detected():
    """全树递归：藏在 list-of-dict-of-list 深处也要抓出来。"""
    env = make_envelope()
    env["data"] = {"factors": [{"name": "mom", "series": [1.0, 2.0, float("nan")]}]}
    problems = find_non_finite(env)
    assert any("factors[0].series[2]" in p for p in problems), problems


def test_non_finite_in_meta_detected():
    env = make_envelope()
    env["meta"]["elapsed_ms"] = float("inf")
    problems = validate_envelope(env)
    assert any("NF-12" in p for p in problems), problems


def test_stringified_nan_detected():
    """把 NaN 转成字符串 "NaN" 同样违规——Agent 侧解析不出数值。"""
    env = make_envelope()
    env["data"]["items"][0]["pe"] = "NaN"
    problems = validate_envelope(env)
    assert any("NF-12" in p for p in problems), problems


def test_raw_json_nan_literal_detected():
    """Python json.dumps 默认写出裸 NaN token —— 标准解析器读不了。"""
    text = json.dumps(
        make_envelope(data={"pe": float("nan")})
    )  # 默认 allow_nan=True，产出 NaN 字面量
    assert "NaN" in text  # 前置确认样本确实踩坑
    problems = validate_envelope_json(text)
    assert any("非法字面量" in p for p in problems), problems


def test_raw_json_infinity_literal_detected():
    text = json.dumps(make_envelope(data={"ratio": float("inf")}))
    problems = validate_envelope_json(text)
    assert any("非法字面量" in p for p in problems), problems


def test_word_nan_inside_normal_string_not_false_positive():
    """字符串内容里含 'nan' 的普通文本不该误报（如公司名 Nanjing）。"""
    env = make_envelope()
    env["data"] = {"name": "Nanjing Bank", "note": "Infinity Group Ltd"}
    text = json.dumps(env, ensure_ascii=False)
    problems = validate_envelope_json(text)
    assert problems == [], problems


# --------------------------------------------------------------------------
# NF-12：浮点 6 位小数
# --------------------------------------------------------------------------


def test_overprecise_float_detected():
    env = make_envelope()
    env["data"]["items"][0]["pe"] = 5.1234567891
    problems = validate_envelope(env)
    assert any("小数位" in p for p in problems), problems


def test_exactly_six_decimals_allowed():
    env = make_envelope()
    env["data"]["items"][0]["pe"] = 5.123456
    assert validate_envelope(env) == []


def test_binary_float_artifact_not_false_positive():
    """0.1+0.2 这类二进制误差要能容忍到 round 之后的正常值。"""
    value = round(0.1 + 0.2, 6)
    env = make_envelope()
    env["data"]["items"][0]["pe"] = value
    assert validate_envelope(env) == [], validate_envelope(env)


def test_integers_are_not_precision_checked():
    env = make_envelope()
    env["data"] = {"count": 1234567890}
    assert validate_envelope(env) == []


def test_find_overprecise_respects_custom_precision():
    env = {"x": 1.23}
    assert find_overprecise_floats(env, precision=1)
    assert not find_overprecise_floats(env, precision=2)


# --------------------------------------------------------------------------
# 便捷断言
# --------------------------------------------------------------------------


def test_assert_envelope_raises_with_all_problems():
    env = make_envelope()
    env.pop("meta")
    env["data"] = {"x": float("nan")}
    with pytest.raises(AssertionError) as exc:
        assert_envelope(env)
    msg = str(exc.value)
    assert "NF-9" in msg and "NF-12" in msg


def test_validator_accepts_json_text_directly():
    assert validate_envelope(json.dumps(make_envelope())) == []


def test_error_envelope_shape_is_still_validated():
    """错误响应同样要走统一信封（NF-9：所有 JSON 响应结构一致）。"""
    env = {
        "code": 4001,
        "message": "证券类型判定为 UNKNOWN，已拒绝写入并计入隔离区",
        "data": None,
        "meta": {
            "generated_at": "2026-08-01T21:30:00+08:00",
            "data_date": None,
            "market": "CN",
            "elapsed_ms": 8,
            "version": "0.1.0",
        },
    }
    assert validate_envelope(env) == []


def test_math_isfinite_contract_sanity():
    """守住前提假设：本校验器依赖 math.isfinite 语义。"""
    assert not math.isfinite(float("nan"))
    assert not math.isfinite(float("inf"))
    assert math.isfinite(0.0)
