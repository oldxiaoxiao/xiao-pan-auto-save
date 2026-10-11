"""双击启动原生窗口；--serve 只供桌面子进程使用。"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from desktop.runtime import BackendProcess, InstanceLock, user_data_dir


def _parent_alive(pid: int) -> bool:
    if sys.platform != "win32":
        return os.getppid() == pid
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def serve() -> None:
    import uvicorn

    from backend.main import app

    log_path = Path(os.environ["DATA_DIR"]) / "logs" / "desktop-server.log"
    logging.basicConfig(filename=log_path, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=5))
    app.state.desktop_shutdown = lambda: setattr(server, "should_exit", True)
    parent = int(os.environ["XIAO_PAN_PARENT_PID"])

    def watch_parent():
        while not server.should_exit:
            if not _parent_alive(parent):
                server.should_exit = True
                return
            time.sleep(1)

    threading.Thread(target=watch_parent, daemon=True).start()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        Path(os.environ["XIAO_PAN_READY_FILE"]).write_text(json.dumps({"port": listener.getsockname()[1]}))
        server.run(sockets=[listener])


def smoke_test() -> None:
    with tempfile.TemporaryDirectory(prefix="xiao-pan-desktop-smoke-") as scratch:
        backend = BackendProcess(Path(scratch))
        try:
            backend.start()
            health = json.loads(backend.request("/api/health"))
            drivers = json.loads(backend.request("/api/drivers"))
            if not any(d["key"] == "quark" and d["supported"] for d in drivers):
                raise RuntimeError("打包后的夸克驱动缺失")
            if b"<html" not in backend.request("/"):
                raise RuntimeError("打包后的界面缺失")
            # 无控制台的 Windows 程序也通过退出码向 CI 提供验证结果。
            if sys.stdout:
                print(json.dumps({"status": "ok", "version": health["version"], "drivers": len(drivers)}))
        finally:
            backend.stop()


def _show_error(message: str) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "小盘启动失败", 0x10)
    elif sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display alert "小盘启动失败" message {json.dumps(message, ensure_ascii=False)}'], check=False)
    elif sys.stderr:
        print(message, file=sys.stderr)


def main() -> int:
    if "--serve" in sys.argv:
        serve()
        return 0
    if "--smoke-test" in sys.argv:
        smoke_test()
        return 0
    data_dir = user_data_dir()
    backend = BackendProcess(data_dir)
    try:
        with InstanceLock(data_dir):
            backend.start()
            import webview

            from backend import __version__
            from desktop import tray as desktop_tray

            webview.settings["ALLOW_FILE_URLS"] = False
            window = webview.create_window(
                f"小盘自动转存 · {__version__}", backend.bootstrap_url,
                width=1180, height=800, min_size=(780, 560),
                # 关闭改为"隐藏到托盘"，所以不再每次弹确认框；真正的退出走托盘菜单里的「退出程序」。
                confirm_close=False, text_select=True,
            )

            state = {"quitting": False}
            holder: dict = {"icon": None}

            def request_quit() -> None:
                """托盘「退出程序」：停托盘 + 关窗口 → webview.start() 返回 → finally 停服务。"""
                state["quitting"] = True
                desktop_tray.stop(holder.get("icon"))
                try:
                    window.destroy()
                except Exception:  # noqa: BLE001 托盘线程上调用 GUI 可能不合法，失败也要能退出
                    pass

            def on_closing() -> bool:
                """关闭窗口 = 隐藏到托盘，调度与下载继续。

                pywebview 的语义是反直觉的：closing 事件里**返回 False 表示取消本次关闭**
                （events.closing.set() 收集返回值，只要有 False 就 should_cancel）。
                所以"隐藏"必须返回 False；"允许真正关闭"则返回非 False。
                """
                if state["quitting"]:
                    return True
                try:
                    window.hide()
                except Exception:  # noqa: BLE001 隐藏失败就让它正常关掉，别卡在半路
                    pass
                return False

            window.events.closing += on_closing
            holder["icon"] = desktop_tray.start(window, on_quit=request_quit)
            try:
                webview.start(private_mode=True, localization={
                    "global.quitConfirmation": "退出后定时追更和正在进行的下载会停止。确定退出？",
                })
            finally:
                desktop_tray.stop(holder.get("icon"))
    except Exception as exc:
        _show_error(str(exc))
        return 1
    finally:
        backend.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
