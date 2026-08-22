"""data 路由单测（D1–D7）：Job 流程 / 404 / 422 / 501 / 分页边界。

全部用假 DataLake（FakeLake）＋ 真 JobManager（SQLite 在 tmp），
**不发真网络**（契约要求：注入假服务）。
"""
from __future__ import annotations

import datetime as dt
import time

from envelope_validator import assert_envelope

from Kuantix.core.contracts import QuarantineEntry


def _entry(code: str = "600001") -> QuarantineEntry:
    now = dt.datetime(2026, 8, 1, 9, 5, 0)
    return QuarantineEntry(
        code=code,
        market="CN",
        reason="readback_mismatch",
        detail="回读价格差 0.012 > 容差 0.001",
        occurred_at=now,
        last_try=now,
        attempts=2,
    )


def _wait_job(client, job_id: str, url_prefix: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"{url_prefix}/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        assert_envelope(payload)
        status = payload["data"]["status"]
        if status in ("done", "failed", "cancelled"):
            return payload["data"]
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束")


def test_d1_status_shape(client) -> None:
    payload = client.get("/api/v1/data/status").json()
    data = payload["data"]
    assert data["market"] == "CN"
    assert data["data_date"] == "2026-08-01"
    assert data["quarantine_count"] == 0
    assert data["latest_job"] is None


def test_d1_status_latest_job_after_sync(client) -> None:
    payload = client.post(
        "/api/v1/data/sync", json={"mode": "incremental", "market": "CN"}
    ).json()
    job = payload["data"]
    assert job["action"] == "sync_incremental"
    assert job["status"] in ("queued", "running", "done")
    _wait_job(client, job["job_id"], "/api/v1/data/sync")
    status_payload = client.get("/api/v1/data/status").json()
    latest = status_payload["data"]["latest_job"]
    assert latest is not None
    assert latest["job_id"] == job["job_id"]


def test_d2_sync_job_lifecycle(client) -> None:
    # force=true：规避 NF-28 交易时段限制，避免用例依赖真实时钟（R5 flaky 修复）
    payload = client.post(
        "/api/v1/data/sync",
        json={"mode": "full", "market": "CN", "years": 5, "force": True},
    ).json()
    assert payload["code"] == 0
    job = payload["data"]
    assert job["module"] == "data"
    assert job["action"] == "sync_full"
    assert job["market"] == "CN"
    keys = ("job_id", "status", "progress", "result_summary", "error",
            "created_at", "updated_at")
    for key in keys:
        assert key in job
    done = _wait_job(client, job["job_id"], "/api/v1/data/sync")
    assert done["status"] == "done"
    assert done["result_summary"]["total"] == 10
    assert done["result_summary"]["done"] == 10


def test_d2_sync_invalid_mode_400(client) -> None:
    response = client.post("/api/v1/data/sync", json={"mode": "bogus"})
    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == 400


def test_d2_sync_market_hk_501(client) -> None:
    response = client.post("/api/v1/data/sync", json={"mode": "full", "market": "HK"})
    assert response.status_code == 501
    assert response.json()["code"] == 501


def test_d3_sync_job_not_found_404(client) -> None:
    response = client.get("/api/v1/data/sync/job_does_not_exist")
    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == 404
    assert "job_does_not_exist" in payload["message"]


def test_d4_cancel_job(client) -> None:
    # FakeLake 的 handle 恒为 done：先起一个 job 等它 done，再 cancel → 422
    # force=true：规避 NF-28 交易时段限制，避免用例依赖真实时钟（R5 flaky 修复）
    payload = client.post(
        "/api/v1/data/sync", json={"mode": "full", "market": "CN", "force": True}
    ).json()
    job_id = payload["data"]["job_id"]
    done = _wait_job(client, job_id, "/api/v1/data/sync")
    assert done["status"] == "done"
    response = client.post(f"/api/v1/data/sync/{job_id}/cancel")
    assert response.status_code == 422
    assert response.json()["code"] == 422


def test_d4_cancel_unknown_404(client) -> None:
    response = client.post("/api/v1/data/sync/job_zzz/cancel")
    assert response.status_code == 404


def test_d5_verify_report(client) -> None:
    payload = client.get("/api/v1/data/verify").json()
    data = payload["data"]
    assert data["market"] == "CN"
    assert data["missing_days"] == []
    assert data["corrupt"] == []
    assert data["quarantined"] == []
    assert data["excluded_count"] == 0
    assert payload["meta"]["data_date"] == "2026-08-01"


def test_d5_verify_hk_501(client) -> None:
    response = client.get("/api/v1/data/verify?market=US")
    assert response.status_code == 501
    assert response.json()["code"] == 501


def test_d6_quarantine_pagination(client, services) -> None:
    # 注入两条隔离区记录
    services.lake._entries = [_entry("600001"), _entry("600002")]
    payload = client.get("/api/v1/data/quarantine?page=1&page_size=1").json()
    data = payload["data"]
    assert data["total"] == 2
    assert data["total_pages"] == 2
    assert len(data["items"]) == 1
    assert data["items"][0]["code"] == "600001"
    assert data["items"][0]["reason"] == "readback_mismatch"
    assert "attempts" in data["items"][0]


def test_d7_quarantine_remove(client, services) -> None:
    services.lake._entries = [_entry("600001")]
    response = client.delete("/api/v1/data/quarantine/600001")
    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] == {"removed": "600001", "reason": "readback_mismatch"}
    assert services.lake.list_quarantine("CN") == []


def test_d7_quarantine_remove_not_found_404(client) -> None:
    response = client.delete("/api/v1/data/quarantine/999999")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_pagination_boundary_page_size_0_400(client) -> None:
    response = client.get("/api/v1/data/quarantine?page_size=0")
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_pagination_boundary_page_0_400(client) -> None:
    response = client.get("/api/v1/data/quarantine?page=0")
    assert response.status_code == 400
    assert response.json()["code"] == 400


# ---------------------------------------------------------------------------
# v1.4：D1 last_sync / schedule 可观测（设计二 D2.6）
# ---------------------------------------------------------------------------


def test_d1_status_has_last_sync_and_schedule(client) -> None:
    """D1 响应含 last_sync/schedule（可空字段，向后兼容只增）。"""
    payload = client.get("/api/v1/data/status").json()
    data = payload["data"]
    assert "last_sync" in data
    assert data["last_sync"] is None  # 无同步记录 → null
    assert "schedule" in data
    schedule = data["schedule"]
    assert schedule["enabled"] is False  # conftest 关调度
    assert schedule["time"] == "16:30"
    assert schedule["startup_check"] is False


def test_d1_status_passthrough_last_sync(client, services) -> None:
    """FakeLake 带 last_sync 记录 → D1 原样透传（路由层不丢字段）。"""
    services.lake._last_sync = {
        "at": "2026-08-03T16:30:12+08:00",
        "status": "done",
        "trigger": "manual",
        "error": None,
        "result": {"total": 10, "done": 10, "failed": 0, "quarantined": 0,
                   "skipped_resumed": 0, "elapsed_ms": 5},
    }
    payload = client.get("/api/v1/data/status").json()
    data = payload["data"]
    assert data["last_sync"]["status"] == "done"
    assert data["last_sync"]["trigger"] == "manual"
    assert data["last_sync"]["result"]["total"] == 10


def test_d1_status_schedule_shape(client) -> None:
    """schedule 字段形状：enabled/time/startup_check（契约 v1.4）。"""
    payload = client.get("/api/v1/data/status").json()
    schedule = payload["data"]["schedule"]
    assert set(schedule) == {"enabled", "time", "startup_check"}


def test_d2_sync_done_writes_manual_sync_state(client, tmp_path) -> None:
    """D2 手动同步（FakeLake done）→ 写 sync_state（trigger=manual，D2.6）。"""
    from Kuantix.data.sync_state import SYNC_STATE_FILENAME, SyncStateStore

    state_file = tmp_path / "db" / SYNC_STATE_FILENAME
    assert not state_file.exists()
    payload = client.post(
        "/api/v1/data/sync", json={"mode": "incremental", "market": "CN"}
    ).json()
    job = payload["data"]
    _wait_job(client, job["job_id"], "/api/v1/data/sync")
    state = SyncStateStore(tmp_path / "db").view()
    assert state is not None
    assert state["status"] == "done"
    assert state["trigger"] == "manual"
    assert state["result"]["total"] == 10
