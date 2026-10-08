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

            webview.settings["ALLOW_FILE_URLS"] = False
            webview.create_window(f"小盘自动转存 · {__version__}", backend.bootstrap_url,
                                  width=1180, height=800, min_size=(780, 560), confirm_close=True,
                                  text_select=True)
            webview.start(private_mode=True, localization={
                "global.quitConfirmation": "退出后定时追更和正在进行的下载会停止。确定退出？",
            })
    except Exception as exc:
        _show_error(str(exc))
        return 1
    finally:
        backend.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
