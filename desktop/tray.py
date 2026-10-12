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


def _logo_path() -> Path | None:
    """打包后资源落在 sys._MEIPASS 下，源码运行时在项目根；两种都要找得到。"""
    for base in (Path(__file__).resolve().parent.parent, Path(getattr(sys, "_MEIPASS", ""))):
        candidate = base / "assets" / "logo.png"
        if candidate.is_file():
            return candidate
    return None


def _icon_image():
    """托盘图标：优先用 assets/logo.png，取不到就画一个纯色占位，绝不因图标失败而启动不了。"""
    from PIL import Image, ImageDraw

    found = _logo_path()
    if found is not None:
        try:
            with Image.open(found) as img:
                return img.convert("RGBA").resize((64, 64), Image.LANCZOS).copy()
        except Exception:  # noqa: BLE001 图标损坏时退回占位，不影响主流程
            pass
    fallback = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(fallback).rounded_rectangle([4, 4, 60, 60], radius=14, fill=(37, 99, 235, 255))
    return fallback


def self_check() -> dict:
    """打包自检：确认托盘在**打包产物里**真的可用，而不是只在开发环境里可用。

    只构造对象、不真的弹出托盘——CI 跑在无人值守的 runner 上，没有 GUI 会话，
    真起图标会挂住或直接报无显示。要抓的是两类打包期才会暴露的问题：

    - pystray 按平台**动态**导入后端（_darwin/_win32/...），静态分析抓不到，
      漏了 hiddenimports 就是打包后在目标机 ImportError，而冒烟测试走服务模式、
      根本不会碰到托盘，这个洞一直没被任何检查覆盖；
    - assets/logo.png 没作为数据文件入包，托盘会默默退回占位色块。

    返回各分项结果，任一项 False 即为打包缺陷，由调用方判失败。
    """
    result = {"pystray": False, "logo": False, "menu": False}
    try:
        import pystray
    except Exception as exc:  # noqa: BLE001 导入失败正是要报出来的问题
        print(f"[tray] 自检失败：无法导入 pystray —— {exc}", file=sys.stderr, flush=True)
        return result
    result["pystray"] = True

    if _logo_path() is None:
        print("[tray] 自检：assets/logo.png 未入包，托盘将使用占位图标", file=sys.stderr, flush=True)
    else:
        result["logo"] = True
    try:
        _icon_image()  # 图标解码（Pillow 及其插件）也要能跑通
    except Exception as exc:  # noqa: BLE001
        print(f"[tray] 自检失败：图标构造异常 —— {exc}", file=sys.stderr, flush=True)
        return result

    try:
        pystray.Menu(pystray.MenuItem("打开界面", lambda: None, default=True),
                     pystray.MenuItem("退出程序", lambda: None))
        result["menu"] = True
    except Exception as exc:  # noqa: BLE001
        print(f"[tray] 自检失败：菜单构造异常 —— {exc}", file=sys.stderr, flush=True)
    return result


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
