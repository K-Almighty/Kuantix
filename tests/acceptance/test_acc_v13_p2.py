"""T10 P2 独立验收（v1.3 Settings E1-E2 + NF-20 只读自证）。

方法
----
- 真 TestClient + 真信封管道；E1 用真 known_hosts（注入临时上游路径，不触碰
  ~/.easy_tdx）+ 假 DataLake/JobManager；E2 用假 TdxClientFactory（不发网络）；
- NF-20 只读自证：E1/E2 前后上游 config.json 指纹（sha256/size/mtime）不变，
  独立计算验证；
- 每个 JSON 响应过 tests/redlines/envelope_validator（NF-9/NF-12）。

红线自查：本文件无 ``except: pass`` / 双参 ``.get(k, 默认)``（R4）；全部离线。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from Kuantix.api.deps import ServiceContainer
from Kuantix.api.jobs import JobManager, JobStore
from Kuantix.api.server import create_app
from Kuantix.config import Config, load_config


def _load_envelope_validator():
    path = Path(__file__).resolve().parents[1] / "redlines" / "envelope_validator.py"
    spec = importlib.util.spec_from_file_location("envelope_validator_acc10", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_envelope_validator()


class _FakeFactor:
    def __init__(self) -> None:
        self._factors: list[str] = []

    def list_factors(self) -> list[str]:
        return list(self._factors)

    def list_models(self) -> list[str]:
        return []

    def list_model_handles(self) -> list[Any]:
        return []

    def compute_factors(self, req: Any) -> list[Any]:
        return []

    def report(self, factor: str, market: str = "CN") -> dict[str, Any]:
        return {}

    def combine(self, factors, method, *, name=None, save_model=False, market="CN"):
        return None

    def load_model(self, name: str) -> Any:
        raise LookupError(f"model {name} not found")


class _FakeScreen:
    def list_batches(self, market=None, page=1, page_size=50):
        return {"items": [], "page": page, "page_size": page_size, "total": 0, "total_pages": 0}

    def get_batch(self, batch_id: str):
        return None

    def get_batch_results(self, batch_id, page=1, page_size=50, sort_by="score", order="desc"):
        return None

    def export_json_payload(self, batch_id: str):
        return None

    def export_csv_bytes(self, batch_id: str):
        return None

    def run_batch(self, req, *, pool_codes=None, excluded_codes=None, filters=None, combine="and"):
        return None


class _FakeLake:
    """假 DataLake：E1 的 data 段（status + latest_job）。"""

    def __init__(self) -> None:
        self._status: dict[str, Any] | None = None

    def status(self, market: str = "CN") -> dict[str, Any]:
        if self._status is not None:
            return dict(self._status)
        return {
            "market": market,
            "data_date": "2024-01-05",
            "coverage": {"securities": 2, "files": 2, "bars": 10},
            "quarantine_count": 0,
            "in_sync_window": False,
        }

    def verify_payload(self, market: str = "CN") -> dict[str, Any]:
        return {
            "market": market, "coverage": {}, "missing_days": [],
            "corrupt": [], "quarantined": [],
        }


class _FakeTdxFactory:
    """假 TdxClientFactory：probe_connection 记录参数并返回可控结果（不发网络）。"""

    def __init__(self, *, ok: bool = True, latency_ms: int = 12, error: str | None = None) -> None:
        self._ok = ok
        self._latency = latency_ms
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def probe_connection(
        self, *, kind: str, host: str, port: int, timeout: float
    ) -> dict[str, Any]:
        self.calls.append({"kind": kind, "host": host, "port": port, "timeout": timeout})
        result: dict[str, Any] = {"ok": self._ok}
        if self._ok:
            result["latency_ms"] = self._latency
        else:
            result["error"] = self._error or "connection refused"
        return result


def _make_config(tmp_path: Path) -> Config:
    """构造路径全部指向 tmp 的配置（不触碰 ~/.Kuantix）。"""
    template = Path(__file__).resolve().parents[2] / "Kuantix" / "resources" / "config.default.toml"
    text = template.read_text(encoding="utf-8")
    text = text.replace('root = "~/.Kuantix"', f'root = "{tmp_path / "root"}"')
    for key in ("vipdoc", "factors", "db", "logs", "reports", "exports"):
        text = text.replace(f'{key} = "~/.Kuantix/{key}"', f'{key} = "{tmp_path / key}"')
    target = tmp_path / "config.toml"
    target.write_text(text, encoding="utf-8")
    return load_config(target)


def _fingerprint(path: Path) -> dict[str, Any]:
    """独立计算文件指纹（sha256/size/mtime 秒级）。"""
    if not path.exists():
        return {"exists": False}
    data = path.read_bytes()
    return {
        "exists": True,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mtime_ns": path.stat().st_mtime_ns,
    }


def _make_settings_container(
    tmp_path: Path,
    *,
    upstream_config: dict[str, Any] | None = None,
    factory: _FakeTdxFactory | None = None,
) -> tuple[ServiceContainer, Path, Path]:
    """组合根：真 known_hosts（注入上游路径）+ 假 factory/lake。"""
    from Kuantix.adapters import known_hosts as kh

    upstream_path = tmp_path / "upstream" / "config.json"
    if upstream_config is not None:
        upstream_path.parent.mkdir(parents=True, exist_ok=True)
        upstream_path.write_text(json.dumps(upstream_config), encoding="utf-8")

    config = _make_config(tmp_path)
    # 重定向 known_hosts 模块级上游路径（进程内一次性，测试隔离）
    kh.EASY_TDX_CONFIG_PATH = upstream_path

    jobs = JobManager(JobStore(config.paths.db))
    container = ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=_FakeFactor(),
        screen_service=_FakeScreen(),
        jobs=jobs,
        tdx_factory=factory if factory is not None else _FakeTdxFactory(),
    )
    return container, upstream_path, config.paths.db


@pytest.fixture()
def e1_client(tmp_path):
    container, upstream_path, _ = _make_settings_container(
        tmp_path, upstream_config={"best_host": "180.153.18.170", "known_hosts": ["1.2.3.4"]}
    )
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        yield c, container, upstream_path


@pytest.fixture()
def e2_client(tmp_path):
    factory = _FakeTdxFactory(ok=True, latency_ms=18)
    container, upstream_path, _ = _make_settings_container(tmp_path, factory=factory)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        yield c, container, factory, upstream_path


@pytest.fixture()
def e2_client_fail(tmp_path):
    factory = _FakeTdxFactory(ok=False, error="timed out after 2s")
    container, upstream_path, _ = _make_settings_container(tmp_path, factory=factory)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        yield c, container, factory, upstream_path


# ---------------------------------------------------------------------------
# E1 状态（只读）
# ---------------------------------------------------------------------------


def test_e1_status_shape(e1_client) -> None:
    """E1：read_only=true + config 摘要 + known_hosts + data(D1) + versions。"""
    c, container, upstream_path = e1_client
    before = _fingerprint(upstream_path)
    r = c.get("/api/v1/settings/status")
    after = _fingerprint(upstream_path)
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    data = body["data"]

    assert data["read_only"] is True

    config_summary = data["config"]
    assert "paths" in config_summary
    assert config_summary["default_market"] == "CN"
    assert config_summary["enabled_markets"] == ["CN"]
    assert "tdx" in config_summary
    assert config_summary["tdx"]["use_easy_tdx_known_hosts"] is True
    assert config_summary["config_source"]

    kh_rows = data["known_hosts"]
    assert "items" in kh_rows
    assert kh_rows["upstream_config_untouched"] is True
    assert kh_rows["upstream_available"] is True
    for row in kh_rows["items"]:
        assert row["read_only"] is True
        assert row["host"] and row["port"] and row["kind"]

    lake = data["data"]
    assert lake["market"] == "CN"
    assert "coverage" in lake
    assert "latest_job" in lake

    versions = data["versions"]
    assert versions["Kuantix"]
    assert versions["upstream_easy_tdx"]

    # NF-20：E1 前后指纹不变
    assert before == after, "E1 读取前后上游 config.json 指纹变化（NF-20 违规）"


def test_e1_upstream_untouched_fingerprint(e1_client) -> None:
    """NF-20 自证：E1 不写上游 config.json（sha256/size/mtime 全等）。"""
    c, container, upstream_path = e1_client
    before = _fingerprint(upstream_path)
    c.get("/api/v1/settings/status")
    after = _fingerprint(upstream_path)
    assert before["exists"] and after["exists"]
    assert before["sha256"] == after["sha256"]
    assert before["size"] == after["size"]
    assert before["mtime_ns"] == after["mtime_ns"]


def test_e1_upstream_missing_falls_back_to_builtin(e1_client) -> None:
    """上游 config.json 不存在 → 自动回退项目内置兜底清单（自给自足部署）。

    - ``upstream_available=False``：用户主目录确实没有 ~/.easy_tdx/config.json（合法态）；
    - 但部署不再因此报错/缺失：``upstream_source=builtin``，``known_hosts_merged=True``，
      节点清单已合入内置兜底清单；
    - NF-20 仍成立：内置清单读取前后未改动（``upstream_config_untouched``）。
    """
    c, container, upstream_path = e1_client
    # 删除注入的上游文件，模拟容器/干净主机没有外部项目目录
    upstream_path.unlink()
    r = c.get("/api/v1/settings/status")
    data = r.json()["data"]
    assert data["known_hosts"]["upstream_available"] is False
    assert data["known_hosts"]["upstream_source"] == "builtin"
    assert data["known_hosts"]["known_hosts_merged"] is True
    assert data["known_hosts"]["upstream_config_untouched"] is True


# ---------------------------------------------------------------------------
# E2 连通性测试
# ---------------------------------------------------------------------------


def test_e2_ok(e2_client) -> None:
    """E2 成功：{ok:true, latency_ms} + 信封 code=0 + 参数透传。"""
    c, container, factory, upstream_path = e2_client
    before = _fingerprint(upstream_path)
    r = c.post(
        "/api/v1/settings/test-connection",
        json={"kind": "std", "host": "180.153.18.170", "port": 7709},
    )
    after = _fingerprint(upstream_path)
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    assert body["code"] == 0
    data = body["data"]
    assert data["ok"] is True
    assert data["latency_ms"] == 18
    assert data["host"] == "180.153.18.170"
    assert data["port"] == 7709
    assert data["kind"] == "std"
    assert data["error"] is None

    # 工厂收到显式 kind/host/port/timeout
    assert len(factory.calls) == 1
    call = factory.calls[0]
    assert call["kind"] == "std"
    assert call["host"] == "180.153.18.170"
    assert call["port"] == 7709
    assert call["timeout"] == 2.0

    # NF-20：E2 前后指纹不变
    assert before == after


def test_e2_connection_failure_is_business_result(e2_client_fail) -> None:
    """E2 连接失败：code=0 信封 + {ok:false, error}（业务结果，非 HTTP 错误）。"""
    c, container, factory, upstream_path = e2_client_fail
    r = c.post(
        "/api/v1/settings/test-connection",
        json={"kind": "mac", "host": "10.0.0.1", "port": 7709},
    )
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    assert body["code"] == 0
    data = body["data"]
    assert data["ok"] is False
    assert data["error"] == "timed out after 2s"
    assert data["latency_ms"] is None


def test_e2_invalid_kind_400(e2_client) -> None:
    c, _, _, _ = e2_client
    r = c.post(
        "/api/v1/settings/test-connection",
        json={"kind": "bogus", "host": "1.2.3.4", "port": 7709},
    )
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_e2_missing_host_400(e2_client) -> None:
    c, _, _, _ = e2_client
    r = c.post(
        "/api/v1/settings/test-connection",
        json={"kind": "std", "host": "", "port": 7709},
    )
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_e2_invalid_port_400(e2_client) -> None:
    c, _, _, _ = e2_client
    r = c.post(
        "/api/v1/settings/test-connection",
        json={"kind": "std", "host": "1.2.3.4", "port": 0},
    )
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_e2_no_service_400(tmp_path) -> None:
    """组合根缺 tdx_factory → E2 显式 400（fail-loud）。"""
    from Kuantix.adapters import known_hosts as kh

    upstream_path = tmp_path / "upstream" / "config.json"
    kh.EASY_TDX_CONFIG_PATH = upstream_path
    config = _make_config(tmp_path)
    jobs = JobManager(JobStore(config.paths.db))
    container = ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=_FakeFactor(),
        screen_service=_FakeScreen(),
        jobs=jobs,
    )
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/settings/test-connection",
            json={"kind": "std", "host": "1.2.3.4", "port": 7709},
        )
    assert r.status_code == 400
    assert r.json()["code"] == 400


# ---------------------------------------------------------------------------
# P1 复验：B5 kline 字段名 / O3 heatmap data 字段（修复回归确认）
# ---------------------------------------------------------------------------


def test_p1_b5_kline_field_name_regression(tmp_path) -> None:
    """【P1-1 复验】B5 响应含 kline 键（前端已改读 kline.kline，非 bars）。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_acc_v13_p1 import _make_p1_container

    container, _ = _make_p1_container(tmp_path)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        r = c.get("/api/v1/backtest/kline/600000")
        assert r.status_code == 200
        data = r.json()["data"]
        assert "kline" in data
        assert "bars" not in data
        assert len(data["kline"]) > 0
        bar = data["kline"][0]
        for key in ("date", "open", "high", "low", "close", "vol", "amount"):
            assert key in bar


def test_p1_o3_heatmap_data_field_regression(tmp_path) -> None:
    """【P1-2 复验】O3 heatmap 含 data 稀疏三元组（前端已改读 h.data，非 cells）。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_acc_v13_p1 import _make_p1_container, _o1_payload, _wait_done

    container, _ = _make_p1_container(tmp_path)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        r = c.post("/api/v1/optimize/run", json=_o1_payload())
        job_id = r.json()["data"]["job_id"]
        done = _wait_done(c, job_id)
        assert done["status"] == "done"
        result = c.get(f"/api/v1/optimize/results/{job_id}").json()["data"]
        h = result["heatmap"]
        assert "data" in h
        assert "cells" not in h
        assert len(h["data"]) == 9
        assert all(len(row) == 3 for row in h["data"])
        # 稀疏三元组值非全空（格子有值）
        vals = [row[2] for row in h["data"] if row[2] is not None]
        assert len(vals) > 0
