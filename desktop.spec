import sys
import os
import glob
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


def _good_openssl_pair():
    """返回 (libssl.3.dylib, libcrypto.3.dylib) 的绝对路径。

    通过内嵌版本串区分：真实 OpenSSL 含 b"OpenSSL"，Apple LibreSSL 含
    b"LibreSSL"（后者缺 _SSL_get0_group_name，会让 cryptography 的 rust 绑定
    dlopen 时报 Symbol not found）。用版本串而非 nm 符号表，可兼容被 strip 的库。
    """
    # 顺序：优先 Python 框架自带（arm64 上即真实 OpenSSL 3，已验证可用）；
    # 其次 Homebrew openssl@3（intel 上框架自带的是 LibreSSL，需回退到这里）。
    patterns = [
        str(Path(sys.prefix) / "lib"),
        "/usr/local/opt/openssl*/lib",
        "/opt/homebrew/opt/openssl*/lib",
    ]
    roots = []
    for pat in patterns:
        for d in sorted(glob.glob(pat), reverse=True):
            if d not in roots:
                roots.append(d)
    for base in roots:
        ssl = os.path.join(base, "libssl.3.dylib")
        crypto = os.path.join(base, "libcrypto.3.dylib")
        if not (os.path.exists(ssl) and os.path.exists(crypto)):
            continue
        try:
            with open(ssl, "rb") as fh:
                head = fh.read(2 << 20)
        except Exception:
            continue
        if b"LibreSSL" in head:
            continue  # Apple LibreSSL：缺新符号，跳过
        if b"OpenSSL" not in head:
            continue  # 非 OpenSSL，跳过
        return ssl, crypto
    return None, None


if sys.platform == "darwin":
    # PyInstaller 会把 Python 框架 / 系统的 libssl/libcrypto 一并收进包，在
    # macOS 15(intel) 上该 libssl.3.dylib 实为 Apple LibreSSL，缺少新符号；
    # 而 COLLECT 的同名合并会让坏库覆盖 cryptography 钩子检测到的好库。
    # 策略：先剔除所有自动收集的 libssl*/libcrypto*，再显式加入经符号校验的
    # 真实 OpenSSL 3，保证 _ssl 与 cryptography 在 arm64 / x64 上都拿到正确库。
    a.binaries = [b for b in a.binaries if not b[0].startswith(("libssl", "libcrypto"))]
    ssl, crypto = _good_openssl_pair()
    if ssl:
        a.binaries.append(("libssl.3.dylib", ssl, "BINARY"))
        a.binaries.append(("libcrypto.3.dylib", crypto, "BINARY"))
    else:
        raise SystemExit(
            "未在 macOS 上找到可用的 OpenSSL 3（libssl.3.dylib 缺失 _SSL_get0_group_name）。"
        )

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
