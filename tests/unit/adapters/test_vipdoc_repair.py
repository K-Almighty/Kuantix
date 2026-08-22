"""VipdocWriter 分钟线崩溃恢复（_repair_tail）纯单测（F1 写入侧）。"""

from __future__ import annotations

from pathlib import Path

from Kuantix.adapters.vipdoc_writer import VipdocWriter

REC = 32  # 5 分钟记录字节数


def test_repair_tail_truncates_residual(tmp_path: Path) -> None:
    """文件大小非记录整数倍 → 截断到最近完整边界，返回 True。"""
    p = tmp_path / "x.5"
    p.write_bytes(b"\x00" * 100)  # 100 非 32 整数倍
    changed = VipdocWriter._repair_tail(p, REC)
    assert changed is True
    assert p.stat().st_size == 96


def test_repair_tail_clean_noop(tmp_path: Path) -> None:
    """已对齐文件 → 不截断，返回 False。"""
    p = tmp_path / "x.5"
    p.write_bytes(b"\x00" * 64)
    assert VipdocWriter._repair_tail(p, REC) is False
    assert p.stat().st_size == 64


def test_repair_tail_missing_file_noop(tmp_path: Path) -> None:
    """文件不存在 → 安全返回 False。"""
    assert VipdocWriter._repair_tail(tmp_path / "nope.5", REC) is False
