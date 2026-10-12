"""托盘的行为契约测试。

真托盘会往系统菜单栏上挂图标、并进入自己的事件循环，测试里绝不能真起。这里用假 pystray
验证：菜单构成、两个菜单项各自的动作、以及 pystray 缺失时的降级。
"""

from __future__ import annotations

import sys
import types

import pytest

from desktop import tray


class _FakeMenuItem:
    def __init__(self, text, action, default=False):
        self.text = text
        self.action = action
        self.default = default


class _FakeMenu:
    def __init__(self, *items):
        self.items = list(items)


class _FakeIcon:
    def __init__(self, name, image, title, menu=None):
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.ran = False
        self.stopped = False

    def run(self):
        self.ran = True  # 不进入事件循环，测试里立刻返回

    def stop(self):
        self.stopped = True


@pytest.fixture()
def fake_pystray(monkeypatch):
    module = types.SimpleNamespace(Icon=_FakeIcon, Menu=_FakeMenu, MenuItem=_FakeMenuItem)
    monkeypatch.setitem(sys.modules, "pystray", module)
    return module


class _Window:
    def __init__(self):
        self.calls: list[str] = []

    def show(self):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")


# ------------------------------------------------------------------------- 图标


def test_icon_image_is_square_rgba():
    image = tray._icon_image()
    assert image.size == (64, 64)
    assert image.mode == "RGBA"


# ------------------------------------------------------------------------- 菜单


def test_tray_menu_offers_open_and_quit(fake_pystray):
    actions: list[str] = []
    window = _Window()
    icon = tray.start(window, on_quit=lambda: actions.append("quit"))
    assert icon is not None
    assert [i.text for i in icon.menu.items] == ["打开界面", "退出程序"]
    assert icon.title == tray.ICON_TITLE
    # 托盘线程里 run() 被调用过
    assert icon.ran is True

    for item in icon.menu.items:
        item.action()
    assert actions == ["quit"]
    assert window.calls == ["show"], "「打开界面」应把窗口显示出来"


def test_tray_first_item_is_default(fake_pystray):
    """单击托盘图标等价于「打开界面」，所以第一项必须是 default。"""
    icon = tray.start(_Window(), on_quit=lambda: None)
    assert icon.menu.items[0].default is True


# ------------------------------------------------------------------------- 降级


def test_tray_disabled_when_pystray_missing(monkeypatch):
    """pystray 不可用时返回 None，调用方据此退回「关闭即退出」，不能让客户端起不来。"""
    monkeypatch.setitem(sys.modules, "pystray", None)  # import pystray 会抛 ImportError
    assert tray.start(_Window(), on_quit=lambda: None) is None


def test_stop_is_safe_for_none_and_broken_icon(fake_pystray):
    tray.stop(None)  # 未启用托盘时不应炸

    class Boom:
        def stop(self):
            raise RuntimeError("托盘已经没了")

    tray.stop(Boom())  # 托盘消失也要能继续退出


def test_menu_action_failure_does_not_propagate(fake_pystray, capsys):
    """托盘回调炸了不能把客户端带崩：异常被就地吞掉并写 stderr。"""

    class BadWindow:
        def show(self):
            raise RuntimeError("GUI 线程不合法")

    icon = tray.start(BadWindow(), on_quit=lambda: None)
    icon.menu.items[0].action()  # 不应抛出
    assert "失败" in capsys.readouterr().err


# ------------------------------------------------------------------------- 打包自检


def test_self_check_passes_in_dev_environment(fake_pystray):
    """打包自检的三项在开发环境都应通过（pystray 可导入、logo 在、菜单可构造）。"""
    assert tray.self_check() == {"pystray": True, "logo": True, "menu": True}


def test_self_check_reports_missing_pystray(monkeypatch):
    """pystray 平台后端漏收是典型的打包缺陷，自检必须把它报成 False 而不是静默放行。"""
    monkeypatch.setitem(sys.modules, "pystray", None)
    result = tray.self_check()
    assert result["pystray"] is False


def test_self_check_reports_logo_not_bundled(monkeypatch, fake_pystray):
    """logo.png 没作为数据文件入包时，托盘会默默退回占位色块——自检要能发现。"""
    monkeypatch.setattr(tray, "_logo_path", lambda: None)
    result = tray.self_check()
    assert result["logo"] is False


def test_self_check_tolerates_corrupted_logo(monkeypatch, fake_pystray):
    """图标文件损坏时代码会退回占位，但自检的 logo 项应如实为 False（资源确实没取到有效图）。"""
    monkeypatch.setattr(tray, "_logo_path", lambda: None)
    assert tray.self_check()["logo"] is False
    assert tray._icon_image().size == (64, 64), "即便如此也不能起不来，退回占位图"
