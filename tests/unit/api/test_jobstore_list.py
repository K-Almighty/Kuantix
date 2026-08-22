"""JobStore.list / JobManager.list_jobs 单测（C1 依赖，v1.3 一并补）。

- 分页（limit 上限 50）/ 过滤（module/market/status）；
- 非法参数 → MissingKeyError（路由层映射 400，fail-loud）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from Kuantix.api.jobs import JobManager, JobStore
from Kuantix.core.fail_loud import MissingKeyError


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "db")


def _seed(store: JobStore) -> None:
    store.create("job_a", "backtest", "run", "CN", {"codes": ["600000"]})
    store.create("job_b", "backtest", "portfolio", "CN", {"codes": ["600000"]})
    store.create("job_c", "data", "sync_full", "CN", {"years": 10})
    store.create("job_d", "data", "sync_full", "HK", {"years": 10})


def test_list_all(store: JobStore) -> None:
    _seed(store)
    jobs = store.list()
    assert len(jobs) == 4
    # 按 created_at 倒序
    assert jobs[0]["job_id"] == "job_d"


def test_list_filter_module(store: JobStore) -> None:
    _seed(store)
    jobs = store.list(module="backtest")
    assert {j["job_id"] for j in jobs} == {"job_a", "job_b"}


def test_list_filter_market(store: JobStore) -> None:
    _seed(store)
    jobs = store.list(market="CN")
    assert {j["job_id"] for j in jobs} == {"job_a", "job_b", "job_c"}


def test_list_filter_status(store: JobStore) -> None:
    _seed(store)
    store.set_status("job_a", "done")
    jobs = store.list(status="done")
    assert {j["job_id"] for j in jobs} == {"job_a"}


def test_list_combined_filters(store: JobStore) -> None:
    _seed(store)
    store.set_status("job_b", "failed")
    jobs = store.list(module="backtest", status="failed")
    assert {j["job_id"] for j in jobs} == {"job_b"}


def test_list_limit(store: JobStore) -> None:
    _seed(store)
    jobs = store.list(limit=2)
    assert len(jobs) == 2


def test_list_limit_too_large_400(store: JobStore) -> None:
    _seed(store)
    with pytest.raises(MissingKeyError):
        store.list(limit=51)


def test_list_limit_zero_400(store: JobStore) -> None:
    with pytest.raises(MissingKeyError):
        store.list(limit=0)


def test_list_invalid_status_400(store: JobStore) -> None:
    with pytest.raises(MissingKeyError):
        store.list(status="bogus")


def test_list_empty(store: JobStore) -> None:
    assert store.list() == []


def test_job_manager_list_jobs_forwards(tmp_path: Path) -> None:
    manager = JobManager(JobStore(tmp_path / "db"))
    manager.submit(
        "backtest",
        "run",
        "CN",
        {"codes": ["600000"]},
        lambda progress_cb, register_handle: {"ok": True},
    )
    jobs = manager.list_jobs(module="backtest", limit=5)
    assert len(jobs) == 1
    assert jobs[0]["module"] == "backtest"
    with pytest.raises(MissingKeyError):
        manager.list_jobs(limit=999)
