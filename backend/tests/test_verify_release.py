"""发布包校验脚本的测试。

用真实文件与真实摘要（不是 mock）：脚本的全部价值就在于"算出来的摘要和官方一致"，
如果用假哈希函数去测，等于测了个空壳。文件做小一点，跑得快。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_release.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_release", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vr = _load()


@pytest.fixture()
def pack(tmp_path: Path):
    zip_path = tmp_path / "demo.zip"
    zip_path.write_bytes(b"x" * 2048)
    digest = vr.sha256_of(zip_path)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{digest}  demo.zip\n", encoding="utf-8")
    return zip_path, sums, digest


def test_sha256_matches_known_value(tmp_path: Path):
    """与已知答案对齐：空串的 SHA256 是公开常量，能抓出"读了错东西"这类低级错误。"""
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert (
        vr.sha256_of(empty) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_parse_sums_accepts_gnu_and_binary_marks():
    text = "abc" + "d" * 61 + "  a.zip\n" + "0" * 64 + " *b.zip\n# 注释\n\n"
    parsed = vr.parse_sums(text)
    assert set(parsed) == {"a.zip", "b.zip"}
    assert parsed["b.zip"] == "0" * 64


def test_verify_release_ok(pack, capsys):
    zip_path, _, digest = pack
    assert vr.main([str(zip_path)]) == vr.EXIT_OK
    assert digest in capsys.readouterr().out


def test_verify_release_detects_tampered_file(pack, capsys):
    """改一个字节就该失败——这正是"下载不完整/被篡改"的真实形态。"""
    zip_path, _, _ = pack
    zip_path.write_bytes(b"y" * 2048)
    assert vr.main([str(zip_path)]) == vr.EXIT_MISMATCH
    assert "不一致" in capsys.readouterr().out


def test_verify_release_reports_missing_entry(tmp_path: Path, capsys):
    other = tmp_path / "other.zip"
    other.write_bytes(b"z")
    (tmp_path / "SHA256SUMS").write_text(f"{'0' * 64}  demo.zip\n", encoding="utf-8")
    assert vr.main([str(other)]) == vr.EXIT_MISMATCH
    assert "缺失" in capsys.readouterr().out


def test_verify_release_directory_mode(pack, capsys):
    zip_path, _, _ = pack
    assert vr.main([str(zip_path.parent)]) == vr.EXIT_OK
    assert "demo.zip" in capsys.readouterr().out


def test_verify_release_expected_digest(pack):
    zip_path, _, digest = pack
    assert vr.main([str(zip_path), "--expected", digest]) == vr.EXIT_OK
    assert vr.main([str(zip_path), "--expected", "0" * 64]) == vr.EXIT_MISMATCH


def test_verify_release_usage_errors(tmp_path: Path, capsys):
    assert vr.main([str(tmp_path / "nope.zip")]) == vr.EXIT_USAGE
    lonely = tmp_path / "lonely.zip"
    lonely.write_bytes(b"a")
    # 旁边没有 SHA256SUMS 且没给 --expected：必须明确告诉用户缺什么，而不是静默通过
    assert vr.main([str(lonely)]) == vr.EXIT_USAGE
    assert "SHA256SUMS" in capsys.readouterr().err
    # --expected 只在单文件时可用
    assert vr.main([str(lonely), str(lonely), "--expected", "0" * 64]) == vr.EXIT_USAGE
