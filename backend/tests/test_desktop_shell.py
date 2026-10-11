"""桌面外壳（开机自启 / 系统通知）的行为契约测试。

这些能力会写系统目录、起系统命令，所以这里**全部用替身**验证决策与转义，绝不真碰本机。
"""

from __future__ import annotations

import pytest

from backend.services import desktop_shell as ds


@pytest.fixture(autouse=True)
def _no_real_system(monkeypatch):
    """默认把"跑系统命令"换成记录器，任何用例漏替身也不会真去动系统。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(ds, "_run", lambda cmd: calls.append(cmd))
    return calls


def _as_desktop(monkeypatch, key: str, target):
    monkeypatch.setattr(ds, "desktop_available", lambda: True)
    monkeypatch.setattr(ds, "_platform_key", lambda: key)
    monkeypatch.setattr(ds, "_autostart_target", lambda: target)


# ------------------------------------------------------------------- 环境判定


def test_autostart_unsupported_outside_desktop(monkeypatch):
    """服务端/Compose 下必须如实说不支持，不能给一个按了没用的开关。"""
    monkeypatch.setattr(ds, "desktop_available", lambda: False)
    state = ds.autostart_state()
    assert state["supported"] is False
    assert "服务端" in state["detail"]


def test_notify_unsupported_outside_desktop(monkeypatch):
    monkeypatch.setattr(ds, "desktop_available", lambda: False)
    assert ds.notify("标题", "内容")["ok"] is False


def test_set_autostart_refuses_when_unsupported(monkeypatch):
    monkeypatch.setattr(ds, "desktop_available", lambda: False)
    result = ds.set_autostart(True)
    assert result["ok"] is False and result["enabled"] is False


# ------------------------------------------------------------------- 开机自启


def test_autostart_macos_writes_agent_and_can_remove(tmp_path, monkeypatch):
    target = tmp_path / "LaunchAgents" / f"{ds.LABEL}.plist"
    _as_desktop(monkeypatch, "macos", target)

    assert ds.autostart_state()["enabled"] is False
    result = ds.set_autostart(True)
    assert result["ok"] and result["enabled"]
    body = target.read_text(encoding="utf-8")
    assert "<key>RunAtLoad</key>" in body and "<true/>" in body
    assert ds.LABEL in body
    assert ds.autostart_state()["enabled"] is True

    # 幂等：已开启再开一次不应报错
    assert ds.set_autostart(True)["ok"] is True

    off = ds.set_autostart(False)
    assert off["ok"] and off["enabled"] is False
    assert not target.exists()
    assert ds.autostart_state()["enabled"] is False


def test_autostart_linux_writes_desktop_file(tmp_path, monkeypatch):
    target = tmp_path / "autostart" / "xiao-pan-auto-save.desktop"
    _as_desktop(monkeypatch, "linux", target)
    assert ds.set_autostart(True)["ok"] is True
    body = target.read_text(encoding="utf-8")
    assert body.startswith("[Desktop Entry]") and "X-GNOME-Autostart-enabled=true" in body


def test_autostart_windows_goes_through_powershell(tmp_path, monkeypatch, _no_real_system):
    target = tmp_path / "Startup" / "x.lnk"
    _as_desktop(monkeypatch, "windows", target)
    assert ds.set_autostart(True)["ok"] is True
    assert _no_real_system, "Windows 应通过 PowerShell 建立启动目录快捷方式"
    script = _no_real_system[0][-1]
    assert "CreateShortcut" in script and "powershell" == _no_real_system[0][0].lower()


def test_autostart_failure_is_reported_not_raised(tmp_path, monkeypatch):
    """开启失败要返回可读原因，绝不能抛出去拖垮调用方。"""
    target = tmp_path / "LaunchAgents" / f"{ds.LABEL}.plist"
    _as_desktop(monkeypatch, "macos", target)
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ds, "_write_macos_agent", lambda _t: (_ for _ in ()).throw(OSError("磁盘只读")))
    result = ds.set_autostart(True)
    # 目标已存在 → 走幂等分支，不会触发失败；换个不存在目标再验一次
    assert result["ok"] is True
    target.unlink()
    result = ds.set_autostart(True)
    assert result["ok"] is False and "磁盘只读" in result["message"]


# --------------------------------------------------------------------- 通知


def test_notify_macos_uses_osascript(monkeypatch, _no_real_system):
    monkeypatch.setattr(ds, "desktop_available", lambda: True)
    monkeypatch.setattr(ds, "_platform_key", lambda: "macos")
    assert ds.notify("标题", "内容")["ok"] is True
    assert _no_real_system[0][0] == "osascript"


def test_notify_reports_missing_command(monkeypatch):
    """系统缺少通知命令时给出可读原因，而不是让异常冒出去。"""

    def boom(_cmd):
        raise FileNotFoundError()

    monkeypatch.setattr(ds, "desktop_available", lambda: True)
    monkeypatch.setattr(ds, "_platform_key", lambda: "linux")
    monkeypatch.setattr(ds, "_run", boom)
    result = ds.notify("t", "m")
    assert result["ok"] is False and "notify-send" in result["message"]


# ------------------------------------------------------------------- 转义（防注入）


def test_quote_helpers_neutralize_injection():
    """通知/路径文本可能来自任务名、文件名，必须挡住换行与引号注入。"""
    assert "\n" not in ds._ps_quote("a\nb")
    assert "\n" not in ds._osa_quote("a\nb")
    assert ds._ps_quote("it's") == "'it''s'"
    assert ds._osa_quote('say "hi"') == '"say \\"hi\\""'
    assert ds._xml_escape('<a href="x">&') == "&lt;a href=&quot;x&quot;&gt;&amp;"


def test_notify_text_with_quotes_stays_one_command(monkeypatch, _no_real_system):
    monkeypatch.setattr(ds, "desktop_available", lambda: True)
    monkeypatch.setattr(ds, "_platform_key", lambda: "macos")
    assert ds.notify("标题", '他说"你好"\n然后再见')["ok"] is True
    script = _no_real_system[0][-1]
    assert script.count("\n") == 0, "换行必须被压掉，否则会截断/注入 AppleScript"
    assert '\\"' in script
