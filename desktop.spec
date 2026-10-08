import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH)
datas = [(str(root / "frontend/dist"), "frontend/dist"),
         (str(root / "LICENSE"), "."), (str(root / "docs/desktop.md"), "docs")]
datas += collect_data_files("tzdata")
hidden = collect_submodules("backend.drivers") + ["sqlalchemy.dialects.sqlite", "webview"]
a = Analysis([str(root / "desktop/main.py")], pathex=[str(root)], datas=datas,
             hiddenimports=hidden, excludes=["pytest", "IPython", "matplotlib", "tkinter"],
             binaries=[], hookspath=[], hooksconfig={}, runtime_hooks=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="XiaoPan", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / "assets/logo.png"), disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="XiaoPan")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="XiaoPan.app", icon=str(root / "assets/logo.png"),
                 bundle_identifier="io.github.oldxiaoxiao.xiao-pan-auto-save",
                 info_plist={"CFBundleShortVersionString": "0.1.1", "CFBundleVersion": "0.1.1",
                             "NSHighResolutionCapable": True,
                             "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True}})
