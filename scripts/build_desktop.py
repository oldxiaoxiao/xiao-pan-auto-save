"""在本机平台编译、验证、归档桌面客户端。"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend import __version__  # noqa: E402


def main() -> None:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument(
        "--variant",
        choices=["slim", "full"],
        default="slim",
        help="slim=不随包 aria2c（默认，沿用旧产物名）；full=随包自带 aria2c，产物名加 -aria2 后缀",
    )
    args = parser.parse_args()
    if sys.platform not in {"win32", "darwin"}:
        raise SystemExit("桌面发行包只在 Windows 或 macOS 的原生环境编译。")
    if not args.skip_frontend:
        npm = shutil.which("npm")
        if npm is None:
            raise SystemExit("未找到 npm，请安装 Node.js 22+ 并确认 PATH。")
        subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=ROOT / "frontend", check=True)
        subprocess.run([npm, "run", "build"], cwd=ROOT / "frontend", check=True)
    if not (ROOT / "frontend/dist/index.html").exists():
        raise SystemExit("缺少前端编译产物。")
    # 变体接线：full 先把随包 aria2c 备好（脚本内含 --version 自检，失败即中断，绝不出半成品包）；
    # slim 必须清掉可能残留的同名目录——否则上一轮构建的二进制会悄悄混进精简包。
    aria2_root = ROOT / "assets" / "aria2"
    if args.variant == "full":
        subprocess.run(
            [sys.executable, "scripts/prepare_aria2_binary.py", "--out", str(aria2_root)],
            cwd=ROOT,
            check=True,
        )
    else:
        shutil.rmtree(aria2_root, ignore_errors=True)
    env = dict(os.environ)
    env["DATA_DIR"] = str(ROOT / "build" / "packaging-data")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "desktop.spec"], cwd=ROOT, env=env, check=True)
    binary = ROOT / "dist" / ("XiaoPan.app/Contents/MacOS/XiaoPan" if sys.platform == "darwin" else "XiaoPan/XiaoPan.exe")
    subprocess.run([str(binary), "--smoke-test"], check=True, timeout=180)
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    arch = platform.machine().lower()
    arch = "x64" if arch in {"amd64", "x86_64"} else arch
    os_name = "macos" if sys.platform == "darwin" else "windows"
    # 变体体现在文件名上：publish 会把所有 desktop-* 产物汇总，重名会互相覆盖。
    variant_suffix = "-aria2" if args.variant == "full" else ""
    archive = release / f"xiao-pan-auto-save-{__version__}-{os_name}-{arch}{variant_suffix}.zip"
    if sys.platform == "darwin":
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(ROOT / "dist/XiaoPan.app"), str(archive)], check=True)
    else:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as target:
            for path in sorted((ROOT / "dist/XiaoPan").rglob("*")):
                if path.is_file():
                    target.write(path, path.relative_to(ROOT / "dist"))
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    print(f"桌面编译和服务冒烟验证通过：{archive}")


if __name__ == "__main__":
    main()
