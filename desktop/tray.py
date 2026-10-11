"""系统托盘：让客户端能常驻，并把"关闭窗口"与"退出程序"分成两件事。

pywebview 没有内置托盘 API，这里用 pystray 补上（Pillow 已在桌面依赖里）。托盘跑在
独立线程，主线程继续阻塞在 webview.start() 上。

语义（roadmap「客户端常驻体验」）：
- 关闭窗口 = 隐藏窗口，定时追更与正在进行的下载继续；
- 退出程序 = 真正结束：停服务、回收 aria2 子进程与单实例锁。

托盘里的任何界面调用都包了 try/except：托盘线程不是 GUI 主线程，个别平台上
show/destroy 可能不合法。宁可少一次显示，也不能因为点托盘把客户端点崩。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

ICON_TITLE = "小盘自动转存"


def _icon_image():
    """托盘图标：优先用 assets/logo.png，取不到就画一个纯色占位，绝不因图标失败而启动不了。"""
    from PIL import Image, ImageDraw

    for base in (Path(__file__).resolve().parent.parent, Path(getattr(sys, "_MEIPASS", ""))):
        candidate = base / "assets" / "logo.png"
        if candidate.is_file():
            try:
                with Image.open(candidate) as img:
                    return img.convert("RGBA").resize((64, 64), Image.LANCZOS).copy()
            except Exception:  # noqa: BLE001 图标损坏时退回占位，不影响主流程
                break
    fallback = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(fallback).rounded_rectangle([4, 4, 60, 60], radius=14, fill=(37, 99, 235, 255))
    return fallback


def _safe(action, label: str) -> None:
    try:
        action()
    except Exception as exc:  # noqa: BLE001 托盘回调炸了不该拖垮客户端
        print(f"[tray] {label}失败：{exc}", file=sys.stderr, flush=True)


def start(window, *, on_quit) -> object | None:
    """启动托盘并返回 Icon；pystray 不可用时返回 None（调用方据此退回"关闭即退出"）。"""
    try:
        import pystray
    except Exception as exc:  # noqa: BLE001 没装 pystray 就退回无托盘模式
        print(f"[tray] 未启用托盘：{exc}", file=sys.stderr, flush=True)
        return None

    def open_window() -> None:
        _safe(window.show, "显示窗口")

    icon = pystray.Icon(
        "xiao-pan-auto-save",
        _icon_image(),
        ICON_TITLE,
        menu=pystray.Menu(
            pystray.MenuItem("打开界面", lambda: open_window(), default=True),
            pystray.MenuItem("退出程序", lambda: _safe(on_quit, "退出程序")),
        ),
    )
    # daemon：主进程结束时不必等托盘线程；托盘自己也有 stop()。
    threading.Thread(target=icon.run, name="tray", daemon=True).start()
    return icon


def stop(icon) -> None:
    """停掉托盘图标（退出程序时调用）。"""
    if icon is None:
        return
    try:
        icon.stop()
    except Exception:  # noqa: BLE001 托盘已经不在了也没关系
        pass
