"""factor 路由单测（F1–F6）：列表 / Job 流程 / 404 / 400 / 比例口径。

假 FactorService 提供确定数据，真 JobManager 跑 F2 后台流程（不发网络）。
"""
from __future__ import annotations

import time

from envelope_validator import assert_envelope


def _wait_job(client, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/v1/factor/jobs/{job_id}").json()
        status = payload["data"]["status"]
        if status in ("done", "failed", "cancelled"):
            return payload["data"]
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


def test_f1_list_paginated(client) -> None:
    payload = client.get("/api/v1/factor?page=1&page_size=1").json()
    data = payload["data"]
    assert data["total"] == 2
    assert data["total_pages"] == 2
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["name"] == "momentum_20d"
    assert item["category"] == "动量"
    assert item["source"] == "builtin"
    assert item["status"] == "computed"
    assert item["years"] == [2021, 2022, 2023]


def test_f1_custom_factor_source(client) -> None:
    payload = client.get("/api/v1/factor").json()
    by_name = {item["name"]: item for item in payload["data"]["items"]}
    assert "volume_ratio_5d" in by_name
    assert by_name["volume_ratio_5d"]["source"] == "custom"


def test_f2_compute_job_lifecycle(client) -> None:
    response = client.post(
        "/api/v1/factor/compute",
        json={
            "factors": ["momentum_20d", "volume_ratio_5d"],
            "market": "CN",
            "start": "2021-01-01",
            "end": "2025-12-31",
            "pool": "all",
        },
    )
    assert response.status_code == 200
    job = response.json()["data"]
    assert job["module"] == "factor"
    assert job["action"] == "compute"
    done = _wait_job(client, job["job_id"])
    assert done["status"] == "done"
    assert done["result_summary"]["count"] == 2
    assert "momentum_20d" in done["result_summary"]["factors"]


def test_f2_compute_unknown_factor_404(client) -> None:
    response = client.post(
        "/api/v1/factor/compute", json={"factors": ["not_a_factor"]}
    )
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_f2_compute_invalid_pool_400(client) -> None:
    response = client.post(
        "/api/v1/factor/compute",
        json={"factors": ["momentum_20d"], "pool": "nonsense"},
    )
    assert response.status_code == 400


def test_f2_compute_start_after_end_400(client) -> None:
    response = client.post(
        "/api/v1/factor/compute",
        json={
            "factors": ["momentum_20d"],
            "start": "2025-01-01",
            "end": "2021-01-01",
        },
    )
    assert response.status_code == 400


def test_f2_compute_hk_501(client) -> None:
    response = client.post(
        "/api/v1/factor/compute",
        json={"factors": ["momentum_20d"], "market": "HK"},
    )
    assert response.status_code == 501


def test_f3_job_not_found_404(client) -> None:
    response = client.get("/api/v1/factor/jobs/job_nope")
    assert response.status_code == 404


def test_f4_report_shape_and_ratios(client) -> None:
    payload = client.get("/api/v1/factor/report?name=momentum_20d").json()
    data = payload["data"]
    assert data["factor"] == "momentum_20d"
    assert data["market"] == "CN"
    assert data["start_date"] == "2024-01-01"
    assert data["end_date"] == "2024-01-02"
    assert data["sample_count"] == 2
    # 比例口径：ic_positive_rate ∈ (0,1]；quantile_returns 数组 5 层
    assert 0.0 < data["ic_positive_rate"] <= 1.0
    assert len(data["quantile_returns"]) == 5
    assert data["quantile_returns"][0] == 0.021
    assert data["ic_series"][0]["ic"] == 0.031
    assert payload["meta"]["data_date"] == data["end_date"]


def test_f4_report_missing_name_400(client) -> None:
    response = client.get("/api/v1/factor/report")
    assert response.status_code == 400


def test_f4_report_unknown_factor_404(client) -> None:
    response = client.get("/api/v1/factor/report?name=not_a_factor")
    assert response.status_code == 404


def test_f5_combine_returns_model(client) -> None:
    response = client.post(
        "/api/v1/factor/combine",
        json={
            "factors": ["momentum_20d", "volume_ratio_5d"],
            "method": "ir",
            "save_model": False,
            "model_name": "m_test",
            "market": "CN",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["name"] == "m_test"
    assert data["method"] == "ir"
    assert set(data["weights"]) == {"momentum_20d", "volume_ratio_5d"}
    assert "created_at" in data


def test_f5_combine_unknown_factor_404(client) -> None:
    response = client.post(
        "/api/v1/factor/combine", json={"factors": ["ghost"], "method": "equal"}
    )
    assert response.status_code == 404


def test_f5_combine_invalid_method_400(client) -> None:
    response = client.post(
        "/api/v1/factor/combine",
        json={"factors": ["momentum_20d"], "method": "bogus"},
    )
    assert response.status_code == 400


def test_f6_models_paginated(client) -> None:
    payload = client.get("/api/v1/factor/models").json()
    data = payload["data"]
    assert data["total"] == 1
    assert data["items"][0]["name"] == "m1"
    assert data["items"][0]["method"] == "ir"
    assert_envelope(payload)
