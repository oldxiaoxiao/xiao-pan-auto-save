"""版本检测的行为测试。

出网点只有一个接缝 `_fetch_json`，全部用例都注入假数据——**不联网**：
GitHub 未认证接口限流 60 次/小时，让 CI 去撞它只会得到随机的红。
重点覆盖"查不到时不许抛错"这条硬约束，以及资产匹配、版本比较和缓存。
"""

from __future__ import annotations

import urllib.error

import pytest

from backend import __version__
from backend.services import update_service as us


@pytest.fixture(autouse=True)
def _clean_cache():
    """模块级缓存会跨用例留存：不清会让"缓存命中"用例假绿、也会污染后续用例。"""
    us.reset_cache()
    yield
    us.reset_cache()


def _release(tag="v9.9.9", assets=None):
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/{us.REPO}/releases/tag/{tag}",
        "body": "更新说明",
        "published_at": "2026-10-13T00:00:00Z",
        "assets": assets
        if assets is not None
        else [
            {
                "name": "xiao-pan-auto-save-9.9.9-macos-arm64.zip",
                "size": 100,
                "browser_download_url": "https://example.invalid/a.zip",
            },
            {
                "name": "xiao-pan-auto-save-9.9.9-macos-arm64-aria2.zip",
                "size": 200,
                "browser_download_url": "https://example.invalid/b.zip",
            },
            {
                "name": "xiao-pan-auto-save-9.9.9-windows-x64.zip",
                "size": 300,
                "browser_download_url": "https://example.invalid/c.zip",
            },
            {"name": "SHA256SUMS", "size": 10, "browser_download_url": "https://example.invalid/sums"},
        ],
    }


def _patch(monkeypatch, payload=None, error: Exception | None = None):
    calls = []

    def fake(url: str) -> dict:
        calls.append(url)
        if error is not None:
            raise error
        return payload if payload is not None else _release()

    monkeypatch.setattr(us, "_fetch_json", fake)
    return calls


# ------------------------------------------------------------------ 版本解析


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("v0.3.2", (0, 3, 2)),
        ("0.4.0", (0, 4, 0)),
        ("0.4.0-rc1", (0, 4, 0)),
        ("1.0", (1, 0)),
        ("", (0,)),
        ("not-a-version", (0,)),
    ],
)
def test_parse_version(raw, want):
    """v 前缀与预发布后缀都要能消化：'0.4.0-rc1' 不能因为尾巴非数字就退化成 0。"""
    assert us.parse_version(raw) == want


def test_parse_version_compares_numeric_not_string():
    """字符串比较会让 0.3.10 < 0.3.2（错），元组比较才是对的。"""
    assert us.parse_version("0.3.10") > us.parse_version("0.3.2")


# ------------------------------------------------------------------ 主体行为


def test_check_detects_newer_version(monkeypatch):
    _patch(monkeypatch)
    data = us.check()
    assert data["current"] == __version__
    assert data["latest"] == "9.9.9"
    assert data["has_update"] is True
    assert data["notes"] == "更新说明"
    assert data["reason"] == ""


def test_check_says_up_to_date_when_same_version(monkeypatch):
    _patch(monkeypatch, _release(tag=f"v{__version__}"))
    data = us.check()
    assert data["has_update"] is False
    assert data["latest"] == __version__


def test_check_says_up_to_date_when_running_newer_than_release(monkeypatch):
    """本地跑的是开发版（比发布版新）时不能报"有更新"。"""
    _patch(monkeypatch, _release(tag="v0.0.1"))
    assert us.check()["has_update"] is False


def test_check_never_raises_on_network_failure(monkeypatch):
    """断网/限流必须变成 reason，而不是让界面弹异常。"""
    _patch(monkeypatch, error=us.UpdateError("无法连接更新服务：离线"))
    data = us.check()
    assert data["has_update"] is False
    assert "离线" in data["reason"]


def test_check_reports_rate_limit_reason(monkeypatch):
    err = urllib.error.HTTPError(us.LATEST_API, 403, "rate limited", {}, None)  # noqa: S310
    _patch(monkeypatch, error=us.UpdateError("更新服务返回 403（可能触发了 GitHub 接口限流）"))
    assert err.code == 403
    assert "403" in us.check()["reason"]


def test_check_handles_missing_tag(monkeypatch):
    _patch(monkeypatch, {"assets": []})
    data = us.check()
    assert data["has_update"] is False
    assert data["reason"] == "更新服务未返回版本号"


# ------------------------------------------------------------------ 缓存


def test_check_uses_cache_without_force(monkeypatch):
    calls = _patch(monkeypatch)
    us.check()
    us.check()
    assert len(calls) == 1  # 第二次不该再出门


def test_check_force_bypasses_cache(monkeypatch):
    calls = _patch(monkeypatch)
    us.check()
    us.check(force=True)
    assert len(calls) == 2


def test_cache_is_not_reused_after_reset(monkeypatch):
    calls = _patch(monkeypatch)
    us.check()
    us.reset_cache()
    us.check()
    assert len(calls) == 2


# ------------------------------------------------------------------ 资产匹配


def test_pick_assets_filters_platform_and_prefers_current_variant():
    assets = [
        {"name": "xiao-pan-auto-save-1.0.0-macos-arm64.zip", "size": 1, "browser_download_url": "a"},
        {"name": "xiao-pan-auto-save-1.0.0-macos-arm64-aria2.zip", "size": 2, "browser_download_url": "b"},
        {"name": "xiao-pan-auto-save-1.0.0-windows-x64.zip", "size": 3, "browser_download_url": "c"},
        {"name": "SHA256SUMS", "size": 4, "browser_download_url": "d"},
    ]
    picked = us.pick_assets(assets, "macos-arm64", "slim")
    assert [a["name"] for a in picked] == [
        "xiao-pan-auto-save-1.0.0-macos-arm64.zip",
        "xiao-pan-auto-save-1.0.0-macos-arm64-aria2.zip",
    ]
    assert picked[0]["variant"] == "slim"
    # full 变体时把 -aria2 排前面，但两个都给（用户可能正想换变体）
    full_first = us.pick_assets(assets, "macos-arm64", "full")
    assert full_first[0]["variant"] == "full"


def test_pick_assets_returns_empty_for_unsupported_platform():
    """桌面端不出 Linux 包：匹配不到就返回空，由界面说明"请换镜像"，而不是给错链接。"""
    assets = [{"name": "xiao-pan-auto-save-1.0.0-windows-x64.zip", "size": 1, "browser_download_url": "a"}]
    assert us.pick_assets(assets, "linux", "slim") == []


def test_sums_asset_is_exposed(monkeypatch):
    _patch(monkeypatch)
    assert us.check()["sums"]["name"] == "SHA256SUMS"


def test_platform_key_is_one_of_known():
    assert us.platform_key() in ("windows-x64", "macos-arm64", "macos-x64", "linux")
