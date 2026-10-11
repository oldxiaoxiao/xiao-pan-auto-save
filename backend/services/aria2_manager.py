"""aria2 进程托管：自动发现、按需拉起、状态上报与一键安装。

桌面端用户最常卡在两件事：内置下载器慢，而自己装 aria2c 又要下载、解压、配 RPC，门槛太高。
这里把「能不能用 aria2」变成程序自己回答的问题：

1. find_binary()：随包自带 → 一键安装目录 → PATH → 常见安装路径，找出 aria2c；
2. ensure_running()：RPC 探不到就顺手拉起一个带 RPC 的 aria2c（固定端口 + 可选 secret），
   起来即用——用户不必知道 RPC 是什么，也不用自己去装；
3. status()：把"装没装 / 谁提供的 / 跑没跑 / 监听在哪"报给前端，界面才说得清当前用谁；
4. install()：一键安装（Windows 取官方包，macOS 走 brew，Linux 走包管理器）。

托管只负责"尽力让它可用"，**绝不静默降级**：拉不起就如实回原因，由下载服务转成用户可见提示。
只接管本进程自己拉起的 aria2——用户原有的 aria2 不接管、退出时也不关闭。
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import httpx

from ..config import DATA_DIR

DEFAULT_PORT = 6800
DEFAULT_HOST_PORT = f"127.0.0.1:{DEFAULT_PORT}"
READY_TIMEOUT = 10.0  # 拉起后等 RPC 就绪的最长秒数
_STATUS_TIMEOUT = 2.0  # 状态探测用短超时，别让设置页卡住

# 只保存"本进程拉起来"的那个 aria2，退出时负责收掉。
_managed: dict = {"proc": None, "host_port": "", "started_at": 0.0}

# 最近一次实际使用的下载器及原因，供状态接口回显，避免降级悄无声息。
_last_engine: dict = {"engine": "", "detail": "", "at": 0.0}


# --------------------------------------------------------------------- 基础路径


def _exe_name() -> str:
    return "aria2c.exe" if os.name == "nt" else "aria2c"


def bundled_dir() -> Path | None:
    """随包自带目录：PyInstaller 解包目录优先，源码态看 assets/aria2。目录不存在即没随包。"""
    meipass = getattr(sys, "_MEIPASS", None)
    base = Path(meipass) / "aria2" if meipass else Path(__file__).resolve().parents[2] / "assets" / "aria2"
    return base if base.is_dir() else None


def managed_dir() -> Path:
    """一键安装的落点（同时也是 aria2 日志目录）：数据目录下，跨平台可写。"""
    path = DATA_DIR / "aria2"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _common_paths() -> list[str]:
    if os.name == "nt":
        return [r"C:\Program Files\aria2\aria2c.exe"]
    return ["/opt/homebrew/bin/aria2c", "/usr/local/bin/aria2c", "/usr/bin/aria2c"]


def find_binary() -> tuple[str, str] | None:
    """返回 (可执行文件路径, 来源)。来源：bundled / managed / system / path。"""
    name = _exe_name()
    bundled = bundled_dir()
    if bundled and (bundled / name).is_file():
        return str(bundled / name), "bundled"
    managed = managed_dir() / name
    if managed.is_file():
        return str(managed), "managed"
    which = shutil.which("aria2c")
    if which:
        return which, "system"
    for candidate in _common_paths():
        if Path(candidate).is_file():
            return candidate, "path"
    return None


# ----------------------------------------------------------------------- RPC 探测


def _rpc_url(host_port: str) -> str:
    """与 download_service._rpc_url 同规则：无协议补 http，无路径补 /jsonrpc。"""
    if "://" in host_port:
        scheme, rest = host_port.split("://", 1)
        host, _, path = rest.partition("/")
        return f"{scheme.lower()}://{host}/{path or 'jsonrpc'}"
    return f"http://{host_port}/jsonrpc"


def split_host_port(host_port: str) -> tuple[str, int]:
    raw = (host_port or "").strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0]
    if ":" in raw:
        host, _, port = raw.rpartition(":")
        try:
            return (host or "127.0.0.1"), int(port)
        except ValueError:
            return (host or "127.0.0.1"), DEFAULT_PORT
    return (raw or "127.0.0.1"), DEFAULT_PORT


async def reachable(host_port: str, secret: str = "", timeout: float = 5.0) -> bool:
    """探测 aria2 RPC 是否可用（getVersion 有 result）。地址为空或异常一律 False。"""
    if not (host_port or "").strip():
        return False
    token = [f"token:{secret}"] if secret else []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                _rpc_url(host_port),
                json={"jsonrpc": "2.0", "id": "ping", "method": "aria2.getVersion", "params": token},
            )
        return bool(resp.json().get("result"))
    except Exception:  # noqa: BLE001 任何异常都视为不可达，交给调用方决定降级
        return False


# ----------------------------------------------------------------------- 拉起进程


def _spawn(binary: str, host_port: str, secret: str, download_dir: str) -> tuple[bool, str]:
    """后台拉起带 RPC 的 aria2c。只监听本机，不对外暴露。"""
    _, port = split_host_port(host_port)
    cmd = [
        binary,
        "--enable-rpc=true",
        f"--rpc-listen-port={port}",
        "--rpc-listen-all=false",  # 只听 127.0.0.1，避免把 RPC 暴露到局域网
        "--continue=true",
        "--file-allocation=none",
        f"--dir={download_dir or str(managed_dir())}",
        f"--log={managed_dir() / 'aria2.log'}",
        "--console-log-level=warn",
    ]
    if secret:
        cmd.append(f"--rpc-secret={secret}")
    try:
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            # 脱离控制台：否则桌面端会多弹出一个黑窗，且随主进程退出被强杀。
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 命令为固定参数拼接，无 shell
    except Exception as exc:  # noqa: BLE001
        return False, f"启动 aria2c 失败：{exc}"
    _managed.update({"proc": proc, "host_port": host_port, "started_at": time.time()})
    return True, ""


async def ensure_running(cfg) -> tuple[object | None, str]:
    """保证 aria2 可用：已在跑就直接用，没跑就尝试拉起。

    返回 (可用配置, 说明)；不可用返回 (None, 原因)，由调用方决定降级并如实告知用户。
    cfg 只需是具备 mode/aria2_host_port/aria2_secret/dir 的 dataclass（鸭子类型，避免循环导入）。
    """
    if getattr(cfg, "mode", "") != "aria2":
        return None, "未启用 aria2 模式"

    configured = (getattr(cfg, "aria2_host_port", "") or "").strip()
    host_port = configured or DEFAULT_HOST_PORT
    secret = getattr(cfg, "aria2_secret", "") or ""

    if await reachable(host_port, secret):
        note_engine("aria2", "已连接到 aria2 RPC")
        return cfg, "已连接现有 aria2"

    host, _ = split_host_port(host_port)
    if host not in ("127.0.0.1", "localhost", "::1", ""):
        return None, f"配置的 aria2 地址 {host_port} 不可达（远端地址不会自动拉起）"

    found = find_binary()
    if not found:
        return None, "未找到 aria2c，可点「一键安装」自动装好"

    binary, source = found
    ok, err = _spawn(binary, host_port, secret, getattr(cfg, "dir", ""))
    if not ok:
        return None, err

    waited = 0.0
    while waited < READY_TIMEOUT:
        if await reachable(host_port, secret):
            note_engine("aria2", f"已自动启动 aria2（{source}）")
            new_cfg = cfg if configured == host_port else replace(cfg, aria2_host_port=host_port)
            return new_cfg, f"已自动启动 aria2（{source}）"
        await asyncio.sleep(0.5)
        waited += 0.5
    return None, "aria2 已启动但 RPC 未就绪，请查看 data/aria2/aria2.log"


def stop() -> bool:
    """停掉本进程拉起的 aria2（有则停，无则 no-op）。用户自己的 aria2 不受影响。"""
    proc = _managed.get("proc")
    if proc is None:
        return False
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001 进程已退出时 terminate 会抛，忽略即可
        pass
    _managed["proc"] = None
    return True


atexit.register(stop)


# ----------------------------------------------------------------------- 状态上报


def note_engine(engine: str, detail: str) -> None:
    """记录本次实际使用的下载器（aria2 / builtin）与原因。"""
    _last_engine.update({"engine": engine, "detail": detail, "at": time.time()})


def last_engine() -> dict:
    return dict(_last_engine)


async def status(mode: str = "", host_port: str = "", secret: str = "") -> dict:
    """给界面用的引擎现状：装没装、谁提供的、跑没跑、当前到底在用谁。"""
    found = find_binary()
    binary, source = found if found else ("", "")
    target = (host_port or "").strip() or DEFAULT_HOST_PORT
    proc = _managed.get("proc")
    return {
        "mode": mode or "",
        "installed": bool(found),
        "binary": binary,
        "source": source,
        "host_port": target,
        "running": await reachable(target, secret, timeout=_STATUS_TIMEOUT),
        "managed_by_app": bool(proc and proc.poll() is None),
        "engine": _last_engine.get("engine") or "",
        "detail": _last_engine.get("detail") or "",
    }


# ----------------------------------------------------------------------- 一键安装


_WIN_ZIP = "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"


def install() -> dict:
    """一键安装 aria2c：Windows 取官方包，macOS 用 brew，Linux 用包管理器。

    只在没装过时动手；失败一律返回可读原因，绝不影响下载主流程。
    """
    if find_binary():
        return {"ok": True, "message": "aria2c 已存在，无需安装", "path": find_binary()[0]}

    dest_dir = managed_dir()
    name = _exe_name()
    try:
        if sys.platform == "darwin":
            brew = shutil.which("brew")
            if not brew:
                return {
                    "ok": False,
                    "message": "未检测到 Homebrew，请先安装 brew 后重试，或手动执行：brew install aria2",
                }
            proc = subprocess.run(  # noqa: S603
                [brew, "install", "aria2"], capture_output=True, text=True, timeout=900
            )
            if proc.returncode != 0:
                return {"ok": False, "message": f"brew install aria2 失败：{(proc.stderr or '').strip()[:300]}"}
            return {"ok": True, "message": "已通过 Homebrew 安装 aria2", "path": shutil.which("aria2c") or ""}

        if os.name == "nt":
            return _install_windows(dest_dir, name)

        # Linux / 容器：按可用包管理器依次尝试
        for mgr, args in (
            ("apt-get", ["apt-get", "install", "-y", "aria2"]),
            ("dnf", ["dnf", "install", "-y", "aria2"]),
            ("yum", ["yum", "install", "-y", "aria2"]),
            ("apk", ["apk", "add", "--no-cache", "aria2"]),
        ):
            if not shutil.which(mgr):
                continue
            proc = subprocess.run(args, capture_output=True, text=True, timeout=900)  # noqa: S603
            if proc.returncode == 0:
                return {"ok": True, "message": f"已通过 {mgr} 安装 aria2", "path": shutil.which("aria2c") or ""}
            return {"ok": False, "message": f"{mgr} 安装失败：{(proc.stderr or '').strip()[:300]}"}
        return {"ok": False, "message": "未找到可用包管理器，请手动安装 aria2 后重试"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "安装超时，请稍后重试或手动安装 aria2"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"安装失败：{exc}"}


def _install_windows(dest_dir: Path, name: str) -> dict:
    """下载 aria2 官方 Windows 包，只取 aria2c.exe 落到托管目录。"""
    from urllib.request import urlretrieve

    zip_path = dest_dir / "aria2.zip"
    try:
        urlretrieve(_WIN_ZIP, zip_path)  # noqa: S310 固定官方地址，非用户输入
        with zipfile.ZipFile(zip_path) as zf:
            member = next((m for m in zf.namelist() if m.lower().endswith("aria2c.exe")), None)
            if not member:
                return {"ok": False, "message": "下载包里没找到 aria2c.exe"}
            with zf.open(member) as src, (dest_dir / name).open("wb") as dst:
                shutil.copyfileobj(src, dst)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"下载/解压 aria2 失败：{exc}"}
    finally:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "message": "已下载并安装 aria2c", "path": str(dest_dir / name)}
