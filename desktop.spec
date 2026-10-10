import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)
sys.path.insert(0, str(ROOT))
from backend import __version__  # noqa: E402

root = ROOT
datas = [(str(root / "frontend/dist"), "frontend/dist"),
         (str(root / "LICENSE"), "."), (str(root / "docs/desktop.md"), "docs")]
datas += collect_data_files("tzdata")
hidden = collect_submodules("backend.drivers") + ["sqlalchemy.dialects.sqlite", "webview"]
a = Analysis([str(root / "desktop/main.py")], pathex=[str(root)], datas=datas,
             hiddenimports=hidden, excludes=["pytest", "IPython", "matplotlib", "tkinter"],
             binaries=[], hookspath=[], hooksconfig={}, runtime_hooks=[], noarchive=False)
# macOS：PyInstaller 可能把系统 / Python 框架的 libssl/libcrypto（macOS 上是 LibreSSL，
# 缺 OpenSSL 1.1.1 的 _SSL_get0_group_name 符号）收进包，并被 cryptography 运行时钩子塞进
# DYLD_LIBRARY_PATH，覆盖 cryptography 自带（已静态链接）的 OpenSSL，导致 dlopen 失败。
# cryptography 4x/5x 静态链接自己的 OpenSSL，剔除这些不兼容的底层库即可；
# Python 标准库 _ssl 走系统 /usr/lib 路径，不受影响。
if sys.platform == "darwin":
    a.binaries = [b for b in a.binaries if not b[0].startswith(("libssl", "libcrypto"))]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="XiaoPan", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / "assets/logo.png"), disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="XiaoPan")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="XiaoPan.app", icon=str(root / "assets/logo.png"),
                 bundle_identifier="io.github.oldxiaoxiao.xiao-pan-auto-save",
                 info_plist={"CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
                             "NSHighResolutionCapable": True,
                             "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True}})
