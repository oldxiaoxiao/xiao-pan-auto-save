"""桌面外壳能力：开机自启与系统通知。

只服务**客户端**（`XIAO_PAN_DESKTOP_MODE=1`）。Compose / 服务端部署没有"这台电脑的登录项"，
也没有"当前登录用户的桌面通知"可言，接口在那里如实返回 supported=False，绝不假装成功。

全部走系统原生机制，**不引入新的第三方依赖**：
- 开机自启：Windows 写"启动"目录快捷方式（PowerShell WScript.Shell）；
  macOS 写 `~/Library/LaunchAgents/*.plist`；Linux 写 `~/.config/autostart/*.desktop`。
- 系统通知：macOS `osascript`；Windows PowerShell 托盘气泡；Linux `notify-send`。

两条通用纪律：
1. 失败一律返回可读原因，绝不影响主流程（自启/通知都是锦上添花，挂了不该拖垮客户端）；
2. 任何拼进脚本的文本都必须转义——通知内容可能来自任务名/文件名，属于用户可控输入。
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

LABEL = "io.github.oldxiaoxiao.xiao-pan-auto-save"
APP_NAME = "小盘自动转存"


# --------------------------------------------------------------------- 基础判定


def desktop_available() -> bool:
    """当前是否运行在桌面客户端里。读调用时的值，便于测试替换。"""
    from .. import config

    return bool(config.DESKTOP_MODE)


def _platform_key() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _app_command() -> tuple[str, str]:
    """返回 (可执行文件路径, 参数串)。冻结后 sys.executable 就是客户端本体。"""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    # 源码态：把启动命令带上，免得自启项指向一个裸解释器。
    return sys.executable, "-m desktop.main"


# ----------------------------------------------------------------------- 开机自启


def _autostart_target() -> Path | None:
    """本平台的自启项落点；返回 None 表示该系统没有可用的自启机制。"""
    key = _platform_key()
    if key == "windows":
        appdata = os.getenv("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / r"Microsoft\Windows\Start Menu\Programs\Startup" / f"{APP_NAME}.lnk"
    if key == "macos":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config" / "autostart" / "xiao-pan-auto-save.desktop"


def autostart_state() -> dict:
    """自启现状。supported=False 时界面应隐藏开关并说明原因，而不是给一个按了没用的开关。"""
    if not desktop_available():
        return {"supported": False, "enabled": False, "method": "", "detail": "仅桌面客户端支持（当前为服务端/Compose 运行）"}
    target = _autostart_target()
    if target is None:
        return {"supported": False, "enabled": False, "method": "", "detail": "当前系统未提供开机自启目录"}
    return {
        "supported": True,
        "enabled": target.exists(),
        "method": _platform_key(),
        "path": str(target),
        "detail": "",
    }


def set_autostart(enabled: bool) -> dict:
    """开启/关闭开机自启。幂等：已经是目标状态就直接返回成功。"""
    state = autostart_state()
    if not state["supported"]:
        return {"ok": False, "enabled": False, "message": state["detail"] or "当前环境不支持开机自启"}

    target = _autostart_target()
    if target is None:  # pragma: no cover autostart_state 已挡过
        return {"ok": False, "enabled": False, "message": "当前系统未提供开机自启目录"}

    if not enabled:
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            return {"ok": False, "enabled": True, "message": f"取消开机自启失败：{exc}"}
        return {"ok": True, "enabled": False, "message": "已取消开机自启"}

    if state["enabled"]:
        return {"ok": True, "enabled": True, "message": "开机自启已开启"}

    key = _platform_key()
    try:
        if key == "windows":
            _write_windows_shortcut(target)
        elif key == "macos":
            _write_macos_agent(target)
        else:
            _write_linux_desktop_file(target)
    except Exception as exc:  # noqa: BLE001 自启是锦上添花，挂了要给出原因而不是崩掉
        return {"ok": False, "enabled": False, "message": f"开启开机自启失败：{exc}"}
    return {"ok": True, "enabled": True, "message": "已开启开机自启"}


def _write_windows_shortcut(target: Path) -> None:
    program, args = _app_command()
    target.parent.mkdir(parents=True, exist_ok=True)
    ps = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$s = $ws.CreateShortcut({_ps_quote(str(target))});"
        f"$s.TargetPath = {_ps_quote(program)};"
        f"$s.Arguments = {_ps_quote(args)};"
        f"$s.Description = {_ps_quote(APP_NAME)};"
        "$s.Save()"
    )
    _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])


def _write_macos_agent(target: Path) -> None:
    program, args = _app_command()
    target.parent.mkdir(parents=True, exist_ok=True)
    items = [f"    <string>{_xml_escape(program)}</string>"]
    if args:
        items += [f"    <string>{_xml_escape(a)}</string>" for a in args.split()]
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n" + "\n".join(items) + "\n  </array>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "</dict>\n</plist>\n"
    )
    target.write_text(plist, encoding="utf-8")


def _write_linux_desktop_file(target: Path) -> None:
    program, args = _app_command()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Exec={program} {args}\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )


# ----------------------------------------------------------------------- 系统通知


def notify(title: str, message: str) -> dict:
    """发一条系统通知。桌面端才有意义；失败返回原因，不影响业务。"""
    if not desktop_available():
        return {"ok": False, "message": "仅桌面客户端支持系统通知（当前为服务端/Compose 运行）"}
    key = _platform_key()
    try:
        if key == "macos":
            script = f'display notification {_osa_quote(message)} with title {_osa_quote(title)}'
            _run(["osascript", "-e", script])
        elif key == "windows":
            _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _windows_balloon(title, message)])
        else:
            _run(["notify-send", title, message])
    except FileNotFoundError:
        return {"ok": False, "message": f"当前系统缺少通知命令（{'notify-send' if key == 'linux' else '系统脚本宿主'}）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"发送通知失败：{exc}"}
    return {"ok": True, "message": "已发送"}


def _windows_balloon(title: str, message: str) -> str:
    """Windows 无第三方库时的通知：临时托盘图标 + 气泡提示。

    等托盘常驻（pystray）落地后应改为复用常驻图标，避免这里为了一条通知短暂闪一个图标。
    """
    return (
        "[void] [System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
        "$ni = New-Object System.Windows.Forms.NotifyIcon;"
        "$ni.Icon = [System.Drawing.SystemIcons]::Information;"
        f"$ni.BalloonTipTitle = {_ps_quote(title)};"
        f"$ni.BalloonTipText = {_ps_quote(message)};"
        "$ni.Visible = $true;"
        "$ni.ShowBalloonTip(8000);"
        "Start-Sleep -Seconds 8;"
        "$ni.Dispose()"
    )


# ----------------------------------------------------------------------- 小工具


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, timeout=60, capture_output=True)  # noqa: S603 固定命令 + 转义参数，无 shell


def _ps_quote(value: str) -> str:
    """PowerShell 单引号字符串：内部单引号翻倍，且禁止换行注入。"""
    safe = (value or "").replace("\r", " ").replace("\n", " ")
    return "'" + safe.replace("'", "''") + "'"


def _osa_quote(value: str) -> str:
    """AppleScript 双引号字符串：转义反斜杠与双引号，并压掉换行。"""
    safe = (value or "").replace("\r", " ").replace("\n", " ")
    return '"' + safe.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _xml_escape(value: str) -> str:
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def platform_label() -> str:
    return {"windows": "Windows", "macos": "macOS", "linux": "Linux"}.get(_platform_key(), platform.system())
