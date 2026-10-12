"""本机文件系统：目录浏览与"在文件管理器中打开"。

配合设置页选择下载目录，免去手敲绝对路径。两点要先说清：

1. **这里是"运行本机后端那台机器"的路径**。桌面客户端后端就在用户电脑上，所以看到的
   就是用户自己的盘；Compose 部署时后端在容器里，看到的是容器路径——这与下载根目录的
   语境本来就是一致的（Compose 的路径向来是容器路径），不是两套标准。
2. **只列目录、不列文件**。这个接口是为了挑"下到哪里"，列文件既没必要也容易误伤隐私。

为什么不用原生系统对话框：pywebview 的 js_api 跑在**新建的线程**里；macOS 后端会把对话框
派发回主线程，但 Windows 后端没有 Invoke 封送，从工作线程弹 WinForms/COM 对话框有跨线程
风险，而这类 GUI 行为在 CI 里无法验证。走后端列目录 + 前端渲染，换来的是可测试、可验证。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MAX_ENTRIES = 500  # 硬盘根目录可能有大量条目，别让一次列目录拖住界面


class FsError(Exception):
    """目录不存在 / 不是目录 / 不可读等，一律转成前端能直接展示的中文原因。"""


def _platform_key() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _root_of(path: Path) -> bool:
    """是否分区根目录（Windows 的盘符、类 Unix 的 /）。"""
    if sys.platform == "win32":
        return len(path.parts) <= 2 and path.drive != ""
    return str(path) == os.path.abspath(os.sep)


def default_path() -> str:
    """打开选择器时默认落在哪。优先 HOME，退到当前工作目录。"""
    home = Path.home()
    return str(home) if home.is_dir() else str(Path.cwd())


def roots() -> list[dict]:
    """快捷入口，让用户不用从根目录一路点进去。"""
    home = Path.home()
    items = [{"name": "主目录", "path": str(home)}]
    downloads = home / "Downloads"
    if downloads.is_dir():
        items.append({"name": "下载", "path": str(downloads)})
    for extra in ("/Volumes", "/mnt", "/media"):
        p = Path(extra)
        if p.is_dir():
            items.append({"name": extra, "path": extra})
    if sys.platform == "win32":
        for drive in _windows_drives():
            items.append({"name": f"{drive} 盘", "path": drive})
    return items


def _windows_drives() -> list[str]:
    """列出可用盘符，避免让用户手动猜 "D:\\"。"""
    import string

    return [f"{c}:\\" for c in string.ascii_uppercase if Path(f"{c}:\\").exists()]


def list_dir(path: str = "") -> dict:
    """列出某目录下的**子目录**。

    返回 {path, exists, writable, parent, entries:[{name, path, is_dir}]}。
    """
    target = Path(path) if path.strip() else Path(default_path())
    try:
        target = target.expanduser().resolve()
    except OSError as exc:  # noqa: BLE001 非法路径也要给出人话
        raise FsError(f"路径不可用：{exc}") from exc

    if not target.exists():
        raise FsError(f"目录不存在：{target}")
    if not target.is_dir():
        raise FsError(f"不是目录：{target}")

    entries: list[dict] = []
    try:
        with os.scandir(target) as it:
            for item in it:
                if len(entries) >= MAX_ENTRIES:
                    break
                try:
                    if not item.is_dir(follow_symlinks=False):
                        continue  # 只挑目录；文件不必暴露给选择器
                except OSError:
                    continue  # 无权限判定，直接跳过
                entries.append({"name": item.name, "path": str(Path(item.path)), "is_dir": True})
    except PermissionError as exc:
        raise FsError(f"没有权限读取该目录：{target}") from exc
    except OSError as exc:  # noqa: BLE001
        raise FsError(f"读取目录失败：{exc}") from exc

    entries.sort(key=lambda e: e["name"].lower())
    try:
        writable = os.access(target, os.W_OK)
    except OSError:
        writable = False

    parent = None if _root_of(target) else str(target.parent)
    return {
        "path": str(target),
        "exists": True,
        "writable": writable,
        "parent": parent,
        "entries": entries,
        "truncated": len(entries) >= MAX_ENTRIES,
    }


# --------------------------------------------------------------- 在文件管理器中打开


def _run(cmd: list[str]) -> None:
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603


def reveal(path: str) -> dict:
    """在系统文件管理器里定位到这个文件或文件夹。

    Windows / macOS 支持"选中某个文件"（explorer /select、open -R）；Linux 的
    xdg-open 没有选中语义，退化为打开所在目录，这是平台能力所限，如实告知。
    """
    target = Path((path or "").strip() or ".")
    if not target.exists():
        raise FsError(f"文件或目录不存在：{target}")

    key = _platform_key()
    select_supported = key in ("windows", "macos")
    if target.is_dir():
        cmd = {"windows": ["explorer", str(target)],
               "macos": ["open", str(target)],
               "linux": ["xdg-open", str(target)]}[key]
    elif key == "windows":
        cmd = ["explorer", "/select,", str(target)]
    elif key == "macos":
        cmd = ["open", "-R", str(target)]
    else:
        cmd = ["xdg-open", str(target.parent)]

    try:
        _run(cmd)
    except FileNotFoundError as exc:
        raise FsError(f"未能打开文件管理器：{exc}") from exc
    except OSError as exc:  # noqa: BLE001
        raise FsError(f"打开文件管理器失败：{exc}") from exc

    return {"ok": True, "path": str(target), "select_supported": select_supported}
