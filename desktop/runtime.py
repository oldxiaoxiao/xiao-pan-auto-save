"""用户数据、实例锁和独立后端进程；此模块不导入 GUI。"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


def user_data_dir(platform: str | None = None, env: dict | None = None, home: Path | None = None) -> Path:
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = home or Path.home()
    if env.get("XIAO_PAN_DESKTOP_DATA_DIR"):
        return Path(env["XIAO_PAN_DESKTOP_DATA_DIR"]).expanduser().absolute()
    if platform == "win32":
        return Path(env.get("LOCALAPPDATA", str(home / "AppData" / "Local"))) / "XiaoPan"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "XiaoPan"
    return Path(env.get("XDG_DATA_HOME", str(home / ".local" / "share"))) / "XiaoPan"


class InstanceLock:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "desktop.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("小盘已经运行，请切换到已打开的窗口。") from exc
        return self

    def __exit__(self, *_):
        if self.file:
            if sys.platform == "win32":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None


class BackendProcess:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.absolute()
        self.token = secrets.token_urlsafe(32)
        self.password = secrets.token_urlsafe(32)
        self.ready_file = self.data_dir / f"desktop-ready-{secrets.token_hex(8)}.json"
        self.url = ""
        self.process: subprocess.Popen | None = None
        self._http = build_opener(ProxyHandler({}))

    @property
    def bootstrap_url(self) -> str:
        return f"{self.url}/api/auth/desktop?token={self.token}"

    def request(self, path: str, method: str = "GET") -> bytes:
        auth = base64.b64encode(f"__desktop__:{self.password}".encode()).decode()
        request = Request(self.url + path, method=method, headers={"Authorization": f"Basic {auth}"})
        with self._http.open(request, timeout=2) as response:
            return response.read()

    def start(self, timeout: float = 40) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        logs = self.data_dir / "logs"
        logs.mkdir(exist_ok=True)
        env = dict(os.environ)
        env.update(DATA_DIR=str(self.data_dir), WEBUI_USERNAME="__desktop__", WEBUI_PASSWORD=self.password,
                   XIAO_PAN_DESKTOP_TOKEN=self.token, XIAO_PAN_DESKTOP_MODE="1",
                   XIAO_PAN_READY_FILE=str(self.ready_file), XIAO_PAN_PARENT_PID=str(os.getpid()))
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--serve"]
            cwd = str(self.data_dir)
        else:
            command = [sys.executable, "-m", "desktop.main", "--serve"]
            cwd = str(Path(__file__).resolve().parent.parent)
        with (logs / "desktop-startup.log").open("ab") as output:
            self.process = subprocess.Popen(command, env=env, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"本地服务启动失败，请查看 {logs / 'desktop-server.log'} 和 {logs / 'desktop-startup.log'}")
                try:
                    if self.ready_file.exists():
                        port = json.loads(self.ready_file.read_text())["port"]
                        self.url = f"http://127.0.0.1:{int(port)}"
                        state = json.loads(self.request("/api/auth/status"))
                        if state.get("authenticated"):
                            return
                except (URLError, OSError, ValueError, KeyError):
                    pass
                time.sleep(0.1)
            raise RuntimeError(f"本地服务启动超时，请查看 {logs}")
        except BaseException:
            self.stop()
            raise
        finally:
            self.ready_file.unlink(missing_ok=True)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                if self.url:
                    self.request("/api/auth/desktop-stop", "POST")
                self.process.wait(timeout=8)
            except (URLError, OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self.ready_file.unlink(missing_ok=True)
