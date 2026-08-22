"""API 信封契约测试：代表端点全部通过 envelope_validator（NF-9/NF-12/NF-6）。

逐个端点真调 TestClient，对响应体跑 ``validate_envelope``（NONE 违规），
并抽查关键业务字段的契约形状（分页壳 / 比例口径 / Job 模型）。
"""
from __future__ import annotations

from envelope_validator import assert_envelope


def _hit(client, method: str, url: str, **kwargs):
    response = client.request(method, url, **kwargs)
    assert response.status_code == 200, f"{method} {url} → {response.status_code} {response.text}"
    payload = response.json()
    assert_envelope(payload)
    return payload


def test_envelope_d1_status(client) -> None:
    payload = _hit(client, "GET", "/api/v1/data/status")
    data = payload["data"]
    assert data["market"] == "CN"
    assert data["coverage"]["last_date"] == "2026-08-01"
    assert "quarantine_count" in data
    assert "latest_job" in data
    assert "in_sync_window" in data


def test_envelope_d5_verify(client) -> None:
    payload = _hit(client, "GET", "/api/v1/data/verify")
    data = payload["data"]
    assert data["excluded_count"] == 0
    assert data["coverage"]["bars"] == 4800
    assert set(data["coverage"]) == {
        "securities",
        "files",
        "bars",
        "disk_bytes",
        "first_date",
        "last_date",
    }


def test_envelope_d6_quarantine_paginated(client) -> None:
    payload = _hit(client, "GET", "/api/v1/data/quarantine")
    data = payload["data"]
    for key in ("items", "page", "page_size", "total", "total_pages"):
        assert key in data


def test_envelope_f1_factor_list(client) -> None:
    payload = _hit(client, "GET", "/api/v1/factor")
    data = payload["data"]
    assert data["total"] >= 2
    first = data["items"][0]
    for key in ("name", "category", "display_name", "description", "source", "status", "years"):
        assert key in first


def test_envelope_f4_report(client) -> None:
    payload = _hit(client, "GET", "/api/v1/factor/report?name=momentum_20d")
    data = payload["data"]
    assert data["factor"] == "momentum_20d"
    # 比例口径：0.05 = 5%（禁止 5.0 式百分比）
    assert 0.0 < data["ic_positive_rate"] <= 1.0
    assert len(data["quantile_returns"]) == 5
    # meta.data_date = end_date
    assert payload["meta"]["data_date"] == data["end_date"]


def test_envelope_f6_models(client) -> None:
    payload = _hit(client, "GET", "/api/v1/factor/models")
    data = payload["data"]
    assert data["total"] == 1
    model = data["items"][0]
    assert model["name"] == "m1"
    assert model["method"] == "ir"
    assert isinstance(model["weights"], dict)
    assert abs(sum(model["weights"].values()) - 0.75) < 1e-6


def test_envelope_s1_filters(client) -> None:
    payload = _hit(client, "GET", "/api/v1/screen/filters")
    data = payload["data"]
    assert isinstance(data["items"], list)
    assert len(data["items"]) >= 5
    first = data["items"][0]
    for key in ("type", "condition", "display_name", "description", "params_schema"):
        assert key in first


def test_envelope_s4_batches(client) -> None:
    payload = _hit(client, "GET", "/api/v1/screen/batches")
    data = payload["data"]
    for key in ("items", "page", "page_size", "total", "total_pages"):
        assert key in data


def test_envelope_error_501_hk(client) -> None:
    response = client.get("/api/v1/data/status?market=HK")
    assert response.status_code == 501
    payload = response.json()
    assert_envelope(payload)
    assert payload["code"] == 501
    assert payload["data"]["error_type"] == "NotSupportedError"
    assert payload["meta"]["market"] == "HK"


def test_envelope_error_422_unknown_market(client) -> None:
    response = client.get("/api/v1/data/status?market=XX")
    assert response.status_code == 422
    payload = response.json()
    assert_envelope(payload)
    assert payload["code"] == 422
    assert payload["data"]["error_type"] == "UnknownValueError"


def test_envelope_error_400_page_size(client) -> None:
    response = client.get("/api/v1/data/quarantine?page_size=501")
    assert response.status_code == 400
    payload = response.json()
    assert_envelope(payload)
    assert payload["code"] == 400
    assert "errors" in payload["data"]


def test_envelope_error_404_job(client) -> None:
    response = client.get("/api/v1/data/sync/job_nope")
    assert response.status_code == 404
    payload = response.json()
    assert_envelope(payload)
    assert payload["code"] == 404
