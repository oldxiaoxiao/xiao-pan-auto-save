"""为「完整版（full）」变体准备随包的 aria2c 二进制。

Windows：aria2 官方 Release 直接提供预编译包，下载后只取 `aria2c.exe`（基本自包含）。
macOS：官方只有源码。这里用 brew 装好后，把主程序**及其 brew 依赖递归**拷进目标目录，
       并把绝对路径改写为 `@loader_path` 相对引用——只拷二进制会在目标机直接
       `dyld: Library not loaded`，因为依赖还指向构建机的 /opt/homebrew。

产物目录结构与 `backend/services/aria2_manager.bundled_dir()` 的约定一致：
打包后落在 `_MEIPASS/aria2/aria2c`。
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ARIA2_VERSION = "1.37.0"
WIN_ZIP = (
    f"https://github.com/aria2/aria2/releases/download/release-{ARIA2_VERSION}/"
    f"aria2-{ARIA2_VERSION}-win-64bit-build1.zip"
)


def _exe_name() -> str:
    return "aria2c.exe" if os.name == "nt" else "aria2c"


def _log(msg: str) -> None:
    """打印进度。日志含中文，Windows 控制台默认 cp1252 会抛 UnicodeEncodeError，
    所以 main() 入口已把 stdout 改成 utf-8；这里再兜一层，编码失败就退成 ASCII 可读文本，
    绝不让"打不出日志"变成构建失败。"""
    line = f"[prepare-aria2] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


# --------------------------------------------------------------------- Windows


def _prepare_windows(target: Path) -> Path:
    url = WIN_ZIP
    _log(f"下载 Windows 官方包：{url}")
    zip_path = target / "aria2-download.zip"
    try:
        urllib.request.urlretrieve(url, zip_path)  # noqa: S310 固定官方地址，非用户输入
        with zipfile.ZipFile(zip_path) as zf:
            member = next((m for m in zf.namelist() if m.lower().endswith("aria2c.exe")), None)
            if member is None:
                raise SystemExit("下载包里没找到 aria2c.exe")
            out = target / "aria2c.exe"
            with zf.open(member) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            _log(f"取出 {member} -> {out}")
    finally:
        zip_path.unlink(missing_ok=True)
    return target / "aria2c.exe"


# ----------------------------------------------------------------------- macOS


def _brew_prefix() -> str:
    brew = shutil.which("brew")
    if brew is None:
        raise SystemExit("未找到 Homebrew，无法为 macOS 准备 aria2c")
    out = subprocess.run([brew, "--prefix"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _linked_deps(path: Path) -> list[str]:
    """otool -L 列出的直接依赖（跳过自身那一行）。"""
    out = subprocess.run(["otool", "-L", str(path)], capture_output=True, text=True, check=True).stdout
    deps: list[str] = []
    for line in out.splitlines()[1:]:
        dep = line.strip().split(" (")[0]
        if dep:
            deps.append(dep)
    return deps


def _relocate(target: Path, prefix: str) -> None:
    """把 target 下二进制的 brew 依赖递归拷进来，并改写为 @loader_path 引用。

    队列收敛：被拷进来的 dylib 自身也可能依赖 brew 库，直到没有外部引用为止。
    """
    queue = [p for p in target.iterdir() if p.is_file()]
    seen: set[Path] = set()
    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for dep in _linked_deps(path):
            if not dep.startswith(prefix):
                continue  # /usr/lib 等系统库不拷
            name = Path(dep).name
            dst = target / name
            if not dst.exists():
                shutil.copy2(dep, dst)
                dst.chmod(dst.stat().st_mode | stat.S_IWUSR)
                _log(f"拷入依赖 {name}")
                queue.append(dst)
            subprocess.run(
                ["install_name_tool", "-change", dep, f"@loader_path/{name}", str(path)],
                check=True, capture_output=True,
            )


def _prepare_macos(target: Path) -> Path:
    prefix = _brew_prefix()
    _log(f"brew prefix = {prefix}")
    if not shutil.which("aria2c"):
        _log("安装 aria2（bottled，走预编译包）")
        subprocess.run(["brew", "install", "aria2"], check=True, capture_output=True)
    src = shutil.which("aria2c")
    if not src:
        raise SystemExit("brew install 之后仍找不到 aria2c")
    out = target / "aria2c"
    shutil.copy2(src, out)
    out.chmod(out.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    _log(f"拷入主程序 -> {out}")
    _relocate(target, prefix)
    return out


# ------------------------------------------------------------------------- 入口


def _verify(binary: Path) -> None:
    """打包期必须跑通：能拦住 macOS 漏拷 dylib、Windows 下载到坏包等问题。"""
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"随包 aria2c 无法执行（{exc}），拒绝出半成品包") from exc
    if out.returncode != 0:
        raise SystemExit(f"随包 aria2c 自检失败：{out.stderr.strip()[:300]}")
    _log(f"自检通过：{out.stdout.strip().splitlines()[0]}")


def main() -> None:
    # 与 scripts/build_desktop.py 同款：Windows 控制台默认 cp1252，中文日志会直接抛
    # UnicodeEncodeError 把构建打断。子进程有自己的 stdout，必须自己重配。
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="产物目录，例如 assets/aria2")
    parser.add_argument("--version", default=ARIA2_VERSION)
    args = parser.parse_args()

    if sys.platform not in {"win32", "darwin"}:
        raise SystemExit("随包 aria2c 只面向 Windows / macOS 桌面包；Linux 请用 Compose 的 aria2 profile。")

    target = Path(args.out)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    binary = _prepare_windows(target) if os.name == "nt" else _prepare_macos(target)
    _verify(binary)
    _log(f"完成：{binary}（{binary.stat().st_size} bytes），目录共 {len(list(target.iterdir()))} 个文件")


if __name__ == "__main__":
    main()
