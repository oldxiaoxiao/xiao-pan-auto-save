"""aria2 托管的行为契约：能连就连、连不上才拉起、拉不起必须如实报原因。

本机不一定装了 aria2c，所以这里全部用替身验证决策链，确保"自动启动"和"不静默降级"都成立。
"""

from __future__ import annotations

import os

import pytest

from backend.services import aria2_manager as am
from backend.services.download_service import DownloadSettings


def _cfg(**kw) -> DownloadSettings:
    return DownloadSettings(**kw)


@pytest.fixture(autouse=True)
def _reset_state():
    """清掉模块级状态，避免用例之间互相看见上次的引擎/进程记录。"""
    am._last_engine.update({"engine": "", "detail": "", "at": 0.0})
    am._managed.update({"proc": None, "host_port": "", "started_at": 0.0})
    yield
    am._last_engine.update({"engine": "", "detail": "", "at": 0.0})


# ------------------------------------------------------------------- 可执行文件发现


def test_find_binary_prefers_bundled(tmp_path, monkeypatch):
    name = "aria2c.exe" if os.name == "nt" else "aria2c"
    (tmp_path / name).write_text("#!/bin/sh\n")
    monkeypatch.setattr(am, "bundled_dir", lambda: tmp_path)
    found = am.find_binary()
    assert found is not None
    assert found[1] == "bundled"
    assert found[0].endswith(name)


def test_find_binary_none_when_missing(monkeypatch):
    monkeypatch.setattr(am, "bundled_dir", lambda: None)
    monkeypatch.setattr(am, "managed_dir", lambda: __import__("pathlib").Path("/nonexistent-aria2-dir"))
    monkeypatch.setattr(am.shutil, "which", lambda _n: None)
    monkeypatch.setattr(am, "_common_paths", lambda: [])
    assert am.find_binary() is None


# ------------------------------------------------------------------- 按需拉起


async def test_ensure_running_uses_existing(monkeypatch):
    """已经在跑就直接复用，不再多起一个进程。"""
    spawned = []

    async def up(host_port, secret="", timeout=5.0):
        return True

    monkeypatch.setattr(am, "reachable", up)
    monkeypatch.setattr(am, "_spawn", lambda *a: (spawned.append(a), (True, ""))[1])
    cfg = _cfg(mode="aria2", aria2_host_port="127.0.0.1:6800")
    ready, note = await am.ensure_running(cfg)
    assert ready is cfg
    assert "已连接" in note
    assert not spawned, "已有可用的 aria2 时不该再拉起进程"


async def test_ensure_running_spawns_when_down(monkeypatch):
    """连不上就自动拉起：这是桌面端免手工安装的关键路径。"""
    calls = {"n": 0}

    async def up(host_port, secret="", timeout=5.0):
        calls["n"] += 1
        return calls["n"] > 1  # 拉起之前不可达，拉起之后可达

    monkeypatch.setattr(am, "reachable", up)
    monkeypatch.setattr(am, "find_binary", lambda: ("/fake/aria2c", "system"))
    monkeypatch.setattr(am, "_spawn", lambda b, hp, s, d: (True, ""))
    cfg = _cfg(mode="aria2")
    ready, note = await am.ensure_running(cfg)
    assert ready is not None, "拉起成功后应当返回可用配置"
    assert "已自动启动" in note
    # 未配置地址时自动补默认本机地址，用户不必自己填 RPC
    assert ready.aria2_host_port == am.DEFAULT_HOST_PORT
    assert am.last_engine()["engine"] == "aria2"


async def test_ensure_running_reports_missing_binary(monkeypatch):
    """没装 aria2c 必须如实报原因，交给上层转成用户可见提示（不再静默降级）。"""

    async def down(host_port, secret="", timeout=5.0):
        return False

    monkeypatch.setattr(am, "reachable", down)
    monkeypatch.setattr(am, "find_binary", lambda: None)
    ready, note = await am.ensure_running(_cfg(mode="aria2"))
    assert ready is None
    assert "未找到 aria2c" in note


async def test_ensure_running_never_spawns_for_remote(monkeypatch):
    """远端地址不可达时不要在本机乱起一个 aria2——那是别人的服务。"""

    async def down(host_port, secret="", timeout=5.0):
        return False

    spawned = []
    monkeypatch.setattr(am, "reachable", down)
    monkeypatch.setattr(am, "_spawn", lambda *a: (spawned.append(a), (True, ""))[1])
    monkeypatch.setattr(am, "find_binary", lambda: ("/fake/aria2c", "system"))
    ready, note = await am.ensure_running(_cfg(mode="aria2", aria2_host_port="192.168.1.50:6800"))
    assert ready is None
    assert "不会自动拉起" in note
    assert not spawned


async def test_ensure_running_ignores_builtin_mode():
    ready, note = await am.ensure_running(_cfg(mode="builtin"))
    assert ready is None and "aria2" in note


# ------------------------------------------------------------------- 状态与降级记录


async def test_status_reports_not_installed(monkeypatch):
    async def down(host_port, secret="", timeout=5.0):
        return False

    monkeypatch.setattr(am, "find_binary", lambda: None)
    monkeypatch.setattr(am, "reachable", down)
    st = await am.status("aria2", "", "")
    assert st["installed"] is False
    assert st["running"] is False
    assert st["host_port"] == am.DEFAULT_HOST_PORT


async def test_note_engine_records_fallback_reason():
    """降级原因必须留在状态里，界面才解释得清为什么这次用了内置。"""
    am.note_engine("builtin", "aria2 不可用：未找到 aria2c")
    state = am.last_engine()
    assert state["engine"] == "builtin"
    assert "未找到 aria2c" in state["detail"]


def test_split_host_port_defaults():
    assert am.split_host_port("") == ("127.0.0.1", am.DEFAULT_PORT)
    assert am.split_host_port("127.0.0.1:6900") == ("127.0.0.1", 6900)
    assert am.split_host_port("http://127.0.0.1:6800/jsonrpc") == ("127.0.0.1", 6800)
