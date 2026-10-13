"""版本检测：本机版本与 GitHub 上最新发布版比对。

只做**告知**，不做自动下载/替换——是否升级由人决定，这是路线图里写定的边界
（"是否启用自动更新由用户选择"）。配套地提供升级前快照（prebackup），
让"升级后数据还在"不靠运气。

两条硬约束：
1. **绝不因网络问题抛错**。查不到就如实说"查不到"，界面显示原因，而不是弹一个
   与用户无关的堆栈。GitHub API 未认证限流是 60 次/小时，故带缓存且缓存期较长。
2. **外部调用收在一个接缝 `_fetch_json` 上**，测试注入假的发布数据即可，CI 不需要联网。
"""

from __future__ import annotations

import json
import platform as _platform
import re
import time
import urllib.error
import urllib.request
from typing import Any

REPO = "oldxiaoxiao/xiao-pan-auto-save"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"

# GitHub 未认证限流 60 次/小时：缓存 6 小时足够，且把"是否真去联网"做成 force 参数。
CACHE_TTL = 6 * 3600
TIMEOUT = 6

_cache: dict[str, Any] = {"at": 0.0, "data": None}


class UpdateError(Exception):
    """更新检查失败。只用于内部流转，接口层一律转成 result 里的 reason。"""


# ------------------------------------------------------------------ 版本与平台


def parse_version(value: str) -> tuple[int, ...]:
    """把 'v0.3.2' / '0.4.0-rc1' 之类解析成可比较元组；非数字段按 0 处理。

    与 backup_service._version_tuple 同思路，但这里要能吃掉 'v' 前缀和预发布后缀，
    否则 '0.4.0-rc1'.split('.') 会把 '0-rc1' 这种尾巴算进去。
    """
    text = (value or "").strip().lstrip("vV")
    text = re.split(r"[-+]", text, maxsplit=1)[0]  # 去掉 -rc1 / +build
    parts: list[int] = []
    for chunk in text.split(".")[:3]:
        digits = re.match(r"\d+", chunk.strip())
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts) if parts else (0,)


def platform_key() -> str:
    """当前运行的桌面平台标识，与发布产物名里的那段一致。

    桌面端不出 Linux 包，Linux 返回 "linux" 由调用方说明"请换镜像"。
    """
    if _platform.system() == "Windows":
        return "windows-x64"
    if _platform.system() == "Darwin":
        machine = _platform.machine().lower()
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x64"
    return "linux"


def current_variant() -> str:
    """当前是随包 aria2c 的 full 还是精简的 slim —— 决定推荐哪个包。

    随包目录存在即 full（见 aria2_manager.bundled_dir）；判断失败退回 slim，
    宁可推荐错，也不因为一个探测异常让整个检查失败。
    """
    try:
        from .aria2_manager import bundled_dir

        return "full" if bundled_dir() else "slim"
    except Exception:  # noqa: BLE001 探测失败不该影响版本检查
        return "slim"


def _asset_variant(name: str) -> str:
    return "full" if "-aria2" in name else "slim"


def pick_assets(assets: list[dict], key: str, variant: str) -> list[dict]:
    """挑出本平台可用的包：先按平台过滤，再把当前变体排在最前。

    两个变体都返回——用户可能正想从 slim 换到 full，不替他决定。
    """
    matched = []
    for a in assets or []:
        name = a.get("name") or ""
        if not name.endswith(".zip"):
            continue
        if key not in name:
            continue
        matched.append(
            {
                "name": name,
                "size": int(a.get("size") or 0),
                "variant": _asset_variant(name),
                "url": a.get("browser_download_url") or "",
            }
        )
    matched.sort(key=lambda a: (a["variant"] != variant, a["name"]))
    return matched


def _sums_asset(assets: list[dict]) -> dict | None:
    for a in assets or []:
        name = a.get("name") or ""
        if name.upper() == "SHA256SUMS":
            return {"name": name, "url": a.get("browser_download_url") or ""}
    return None


# ---------------------------------------------------------------------- 网络接缝


def _fetch_json(url: str) -> dict:
    """唯一的出网点。测试 monkeypatch 这个函数，CI 不联网也能覆盖全部分支。"""
    req = urllib.request.Request(  # noqa: S310 地址是本仓库固定地址，非用户输入
        url,
        headers={"accept": "application/vnd.github+json", "user-agent": "xiao-pan-auto-save"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"更新服务返回 {exc.code}（可能触发了 GitHub 接口限流）") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"无法连接更新服务：{exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpdateError("更新服务返回的内容无法解析") from exc


# ---------------------------------------------------------------------- 对外主流程


def check(force: bool = False) -> dict:
    """比对本机版本与最新发布版。

    返回 {current, latest, has_update, url, notes, published_at, assets, sums,
    platform, variant, reason, checked_at}。查不到时 latest 为空、has_update=False、
    reason 说明原因——**不抛异常**，调用方不必写 try。
    """
    from .. import __version__

    now = time.monotonic()
    if not force and _cache["data"] and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]

    base = {
        "current": __version__,
        "latest": "",
        "has_update": False,
        "url": RELEASES_PAGE,
        "notes": "",
        "published_at": "",
        "assets": [],
        "sums": None,
        "platform": platform_key(),
        "variant": current_variant(),
        "reason": "",
        "checked_at": int(time.time()),
    }

    try:
        payload = _fetch_json(LATEST_API)
    except UpdateError as exc:
        base["reason"] = str(exc)
        result = base
    else:
        latest = str(payload.get("tag_name") or payload.get("name") or "").strip()
        result = {
            **base,
            "latest": latest.lstrip("vV"),
            "has_update": parse_version(latest) > parse_version(base["current"]),
            "url": payload.get("html_url") or RELEASES_PAGE,
            "notes": (payload.get("body") or "").strip(),
            "published_at": payload.get("published_at") or "",
            "assets": pick_assets(payload.get("assets") or [], base["platform"], base["variant"]),
            "sums": _sums_asset(payload.get("assets") or []),
        }
        if not latest:
            result["reason"] = "更新服务未返回版本号"

    _cache["at"] = now
    _cache["data"] = result
    return result


def reset_cache() -> None:
    """仅供测试：清掉进程内缓存，避免用例之间互相污染。"""
    _cache["at"] = 0.0
    _cache["data"] = None
