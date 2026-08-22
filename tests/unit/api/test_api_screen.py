"""screen 路由单测（S1–S6）：条件清单 / 批次 Job 流程 / 结果查询 / 导出。

批次流程（S2→S3→S4→S5→S6）用**真 ScreenService**（假 store/reader，
真批次落盘 tmp）＋ 真 JobManager，完整覆盖 run→poll→results→export，
不发真网络。
"""
from __future__ import annotations

import time

from envelope_validator import assert_envelope


def _wait_job(client, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/v1/screen/jobs/{job_id}").json()
        status = payload["data"]["status"]
        if status in ("done", "failed", "cancelled"):
            return payload["data"]
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


def _run_screen(client, **overrides) -> dict:
    body = {"model": None, "market": "CN", "pool": "all", "top_n": 50, "filters": []}
    body.update(overrides)
    response = client.post("/api/v1/screen/run", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_s1_filters_list(screen_client) -> None:
    payload = screen_client.get("/api/v1/screen/filters").json()
    items = payload["data"]["items"]
    types = {item["type"] for item in items}
    assert types == {"tech", "chanlun"}
    conditions = {item["condition"] for item in items}
    assert "ma_fast" in conditions
    assert "require_buy_point" in conditions
    for item in items:
        assert "params_schema" in item


def test_s2_s3_screen_run_job_flow(screen_client) -> None:
    job = _run_screen(screen_client)
    assert job["module"] == "screen"
    assert job["action"] == "run"
    done = _wait_job(screen_client, job["job_id"])
    assert done["status"] == "done"
    summary = done["result_summary"]
    assert set(summary) == {"batch_id", "result_count", "excluded_count", "as_of"}
    assert summary["result_count"] == 2
    assert summary["excluded_count"] == 0
    assert summary["as_of"] == "2024-01-01"
    assert summary["batch_id"].startswith("batch_")


def test_s2_run_model_not_found_404(screen_client) -> None:
    response = screen_client.post(
        "/api/v1/screen/run", json={"model": "ghost_model", "market": "CN"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_s2_run_invalid_filter_400(screen_client) -> None:
    response = screen_client.post(
        "/api/v1/screen/run",
        json={"filters": [{"type": "tech", "condition": "bogus_cond", "params": {}}]},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_s2_run_invalid_filter_type_400(screen_client) -> None:
    response = screen_client.post(
        "/api/v1/screen/run",
        json={"filters": [{"type": "fundamental", "condition": "x", "params": {}}]},
    )
    assert response.status_code == 400


def test_s2_run_ma_cross_alias(screen_client) -> None:
    """契约示例条件 ma_cross 等价映射为 ma_fast + ma_slow。"""
    job = _run_screen(
        screen_client,
        filters=[
            {"type": "tech", "condition": "ma_cross", "params": {"fast": 20, "slow": 60}}
        ],
    )
    done = _wait_job(screen_client, job["job_id"])
    assert done["status"] == "done"


def test_s2_run_ma_fast_endpoint(screen_client) -> None:
    """ma_fast 带 {fast, slow} 端到端可用（不再静默丢参成 True）。"""
    job = _run_screen(
        screen_client,
        filters=[
            {"type": "tech", "condition": "ma_fast", "params": {"fast": 20, "slow": 60}}
        ],
    )
    done = _wait_job(screen_client, job["job_id"])
    assert done["status"] == "done"


def test_s2_run_min_close_bad_params_400(screen_client) -> None:
    """params 非空但缺取值键 → 400（fail-loud，不再静默退回 True）。"""
    response = screen_client.post(
        "/api/v1/screen/run",
        json={
            "filters": [
                {"type": "tech", "condition": "min_close", "params": {"foo": 1}}
            ]
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_translate_ma_cross_maps_both() -> None:
    """ma_cross {fast, slow} → {ma_fast: 20, ma_slow: 60}。"""
    from Kuantix.api.routers.screen import _translate_filters
    from Kuantix.api.schemas import ScreenFilterInput

    tech, chanlun = _translate_filters(
        [
            ScreenFilterInput(
                type="tech", condition="ma_cross", params={"fast": 20, "slow": 60}
            )
        ]
    )
    assert tech == {"ma_fast": 20, "ma_slow": 60}
    assert chanlun == {}


def test_translate_ma_fast_uses_fast_slow() -> None:
    """ma_fast {fast, slow} → {ma_fast: 20, ma_slow: 60}（S1 schema 引导的正确消费）。"""
    from Kuantix.api.routers.screen import _translate_filters
    from Kuantix.api.schemas import ScreenFilterInput

    tech, chanlun = _translate_filters(
        [
            ScreenFilterInput(
                type="tech", condition="ma_fast", params={"fast": 20, "slow": 60}
            )
        ]
    )
    assert tech == {"ma_fast": 20, "ma_slow": 60}
    assert chanlun == {}


def test_translate_ma_slow_requires_slow() -> None:
    """ma_slow 缺 slow 键 → MissingKeyError（fail-loud）。"""
    import pytest

    from Kuantix.api.routers.screen import _translate_filters
    from Kuantix.api.schemas import ScreenFilterInput
    from Kuantix.core.fail_loud import MissingKeyError

    with pytest.raises(MissingKeyError):
        _translate_filters(
            [ScreenFilterInput(type="tech", condition="ma_slow", params={})]
        )


def test_translate_param_value_fail_loud_missing_key() -> None:
    """_param_value：非空 params 缺 value/condition 键 → MissingKeyError。"""
    import pytest

    from Kuantix.api.routers.screen import _param_value
    from Kuantix.core.fail_loud import MissingKeyError

    assert _param_value("require_buy_point", {}) is True  # 空 params = 布尔条件
    assert _param_value("min_close", {"value": 10.0}) == 10.0
    assert _param_value("min_close", {"min_close": 10.0}) == 10.0
    with pytest.raises(MissingKeyError):
        _param_value("min_close", {"foo": 1})


def test_s2_run_pool_watchlist_501(screen_client) -> None:
    response = screen_client.post(
        "/api/v1/screen/run", json={"pool": "watchlist", "market": "CN"}
    )
    assert response.status_code == 501
    assert response.json()["code"] == 501


def test_s3_job_not_found_404(screen_client) -> None:
    response = screen_client.get("/api/v1/screen/jobs/job_nope")
    assert response.status_code == 404


def test_s4_s5_batches_and_results(screen_client) -> None:
    job = _run_screen(screen_client, top_n=50)
    done = _wait_job(screen_client, job["job_id"])
    batch_id = done["result_summary"]["batch_id"]

    batches = screen_client.get("/api/v1/screen/batches?market=CN").json()
    items = batches["data"]["items"]
    assert len(items) == 1
    batch = items[0]
    assert batch["batch_id"] == batch_id
    assert batch["status"] == "done"
    assert batch["result_count"] == 2
    assert batch["excluded_count"] == 0
    assert batch["as_of"] == "2024-01-01"
    assert batch["elapsed_ms"] >= 0

    results = screen_client.get(
        f"/api/v1/screen/results?batch_id={batch_id}&sort_by=score&order=desc"
    ).json()
    data = results["data"]
    assert data["total"] == 2
    assert data["items"][0]["rank"] == 1
    # 评分降序 → 600036 第一
    assert data["items"][0]["code"] == "600036"
    assert data["items"][0]["score"] > data["items"][1]["score"]
    assert results["meta"]["data_date"] == batch["as_of"]

    # 排序方向反转
    asc = screen_client.get(
        f"/api/v1/screen/results?batch_id={batch_id}&sort_by=score&order=asc"
    ).json()
    assert asc["data"]["items"][0]["code"] == "600000"


def test_s5_results_batch_not_found_404(screen_client) -> None:
    response = screen_client.get("/api/v1/screen/results?batch_id=batch_nope")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_s5_results_invalid_sort_400(screen_client) -> None:
    response = screen_client.get(
        "/api/v1/screen/results?batch_id=batch_nope&sort_by=hack"
    )
    # 参数校验先于 batch 存在性：sort_by 非法 → 400
    assert response.status_code == 400


def test_s6_export_csv_gbk_with_disclaimer(screen_client) -> None:
    job = _run_screen(screen_client)
    done = _wait_job(screen_client, job["job_id"])
    batch_id = done["result_summary"]["batch_id"]

    response = screen_client.get(f"/api/v1/screen/results/{batch_id}/export?format=csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "gbk" in response.headers["content-type"].lower()
    assert "attachment" in response.headers["content-disposition"]
    body = response.content.decode("gbk")
    assert "Kuantix 选股结果" in body
    assert "仅供人工核对参考，非自动交易指令" in body
    # 契约 §3.4：6 列（含数据日期），列序对齐契约示例
    assert "代码,名称,最新价,综合得分,触发条件,数据日期" in body
    assert "600036" in body
    data_row = body.strip().splitlines()[2]
    fields = data_row.split(",")
    assert len(fields) == 6
    assert fields[-1] == "2024-01-01"  # as_of 数据日期


def test_s6_export_json_envelope(screen_client) -> None:
    job = _run_screen(screen_client)
    done = _wait_job(screen_client, job["job_id"])
    batch_id = done["result_summary"]["batch_id"]

    response = screen_client.get(f"/api/v1/screen/results/{batch_id}/export?format=json")
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    assert payload["data"]["total"] == 2
    assert "screen_" in response.headers["content-disposition"]
    assert ".json" in response.headers["content-disposition"]


def test_s6_export_invalid_format_400(screen_client) -> None:
    response = screen_client.get(
        "/api/v1/screen/results/batch_nope/export?format=xlsx"
    )
    assert response.status_code == 400


def test_s6_export_batch_not_found_404(screen_client) -> None:
    response = screen_client.get("/api/v1/screen/results/batch_nope/export")
    assert response.status_code == 404
    assert response.json()["code"] == 404
