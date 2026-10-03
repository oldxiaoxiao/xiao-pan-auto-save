"""本地下载：转存成功后把新文件落到本地磁盘。

两种模式（协议对齐原项目 aria2 插件）：
- builtin：服务端 httpx 流式下载，.part 临时文件 + 完成后改名，已存在且同大小则跳过，
  按配置并发数并行；
- aria2：JSON-RPC addUri 投递直链任务（带网盘 Cookie/UA 头），支持 pause 挂起。
下载成功后如配置了 Emby，触发一次媒体库刷新。
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..core.engine import SavedFile
from ..core.logstream import LogFn
from ..drivers.base import CloudDrive, DriveError

DEFAULT_DOWNLOAD_DIR = str(Path(__file__).resolve().parent.parent.parent / "data" / "downloads")


@dataclass
class DownloadSettings:
    mode: str = "builtin"  # builtin | aria2
    dir: str = DEFAULT_DOWNLOAD_DIR
    concurrency: int = 2
    aria2_host_port: str = ""
    aria2_secret: str = ""
    aria2_pause: bool = False
    emby_url: str = ""
    emby_token: str = ""

    @classmethod
    def from_dict(cls, raw: dict | None) -> DownloadSettings:
        raw = raw or {}
        aria2 = raw.get("aria2") or {}
        emby = raw.get("emby") or {}
        return cls(
            mode=str(raw.get("mode") or "builtin"),
            dir=str(raw.get("dir") or DEFAULT_DOWNLOAD_DIR),
            concurrency=max(1, int(raw.get("concurrency") or 2)),
            aria2_host_port=str(aria2.get("host_port") or ""),
            aria2_secret=str(aria2.get("secret") or ""),
            aria2_pause=bool(aria2.get("pause")),
            emby_url=str(emby.get("url") or ""),
            emby_token=str(emby.get("token") or ""),
        )


@dataclass
class _Item:
    fid: str
    name: str
    size: int
    local_path: Path


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name: str) -> str:
    cleaned = _UNSAFE.sub("_", (name or "").strip()).lstrip(".")
    return cleaned[:200] or "_"


def resolve_local(dest_path: str, cfg: DownloadSettings, override: str) -> Path:
    """云端绝对路径 → 本地路径。override 非空时平铺到 dir/override/文件名，否则镜像网盘目录。"""
    if override:
        return Path(
            cfg.dir,
            *[safe_name(p) for p in override.strip("/").split("/") if p],
            safe_name(Path(dest_path).name),
        )
    parts = [safe_name(p) for p in dest_path.split("/") if p and p not in (".", "..")]
    return Path(cfg.dir).joinpath(*parts)


async def collect_files(
    driver: CloudDrive, saved: list[SavedFile], cfg: DownloadSettings, override: str, download_subdir: bool
) -> list[_Item]:
    """待下载清单：文件直接入列；目录在开启递归时深度遍历（本地镜像子结构）。"""
    items: list[_Item] = []
    for f in saved:
        local = resolve_local(f.dest_path, cfg, override)
        if not f.is_dir:
            items.append(_Item(f.new_fid, Path(f.dest_path).name, 0, local))
        elif download_subdir:
            items.extend(await _walk_dir(driver, f.new_fid, local))
    return items


async def _walk_dir(driver: CloudDrive, fid: str, local_dir: Path) -> list[_Item]:
    out: list[_Item] = []
    try:
        children = await driver.list_dir_children(fid)
    except DriveError:
        return out
    for c in children:
        child = local_dir / safe_name(c.name)
        if c.is_dir:
            out.extend(await _walk_dir(driver, c.fid, child))
        else:
            out.append(_Item(c.fid, c.name, c.size, child))
    return out


async def download_task_files(
    driver: CloudDrive,
    saved: list[SavedFile],
    cfg: DownloadSettings,
    *,
    download_subdir: bool = False,
    savepath_override: str = "",
    log: LogFn | None = None,
) -> list[str]:
    """执行下载，返回摘要行（供通知聚合）。单文件失败不中断整体。"""
    log = log or (lambda level, msg: None)
    if not saved:
        return []
    items = await collect_files(driver, saved, cfg, savepath_override, download_subdir)
    if not items:
        return []
    if cfg.mode == "aria2":
        lines = await _aria2_submit(driver, items, cfg, log)
    else:
        lines = await _builtin_download(driver, items, cfg, log)
    if any(line.startswith("✅") for line in lines):
        await _emby_refresh(cfg, log)
    return lines


async def _resolve_links(driver: CloudDrive, items: list[_Item]) -> tuple[dict[str, dict], str]:
    rows, cookie_str = await driver.get_download_urls([i.fid for i in items])
    return {r["fid"]: r for r in rows if r.get("download_url")}, cookie_str


async def _builtin_download(
    driver: CloudDrive, items: list[_Item], cfg: DownloadSettings, log: LogFn
) -> list[str]:
    by_fid, cookie_str = await _resolve_links(driver, items)
    ua = getattr(driver, "UA", "Mozilla/5.0")
    sem = asyncio.Semaphore(cfg.concurrency)

    async def one(item: _Item) -> str:
        row = by_fid.get(item.fid)
        if not row:
            return f"❌ 取直链失败: {item.name}"
        async with sem:
            try:
                ok, msg = await _fetch_one(row, item, cookie_str, ua)
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, f"{item.name}: {exc}"
        log("info" if ok else "warn", f"📥 {msg}")
        return f"{'✅' if ok else '❌'} {msg}"

    return list(await asyncio.gather(*(one(i) for i in items)))


async def _fetch_one(row: dict, item: _Item, cookie_str: str, ua: str) -> tuple[bool, str]:
    path = item.local_path
    path.parent.mkdir(parents=True, exist_ok=True)
    size = int(row.get("size") or item.size or 0)
    if path.exists() and size and path.stat().st_size == size:
        return True, f"跳过（已存在）{item.name}"
    part = path.with_name(path.name + ".part")
    headers = {"user-agent": ua}
    if cookie_str:
        headers["cookie"] = cookie_str
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        async with client.stream("GET", row["download_url"], headers=headers) as resp:
            if resp.status_code != 200:
                return False, f"{item.name}: HTTP {resp.status_code}"
            written = 0
            with part.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 16):
                    fh.write(chunk)
                    written += len(chunk)
    if size and written != size:
        part.unlink(missing_ok=True)
        return False, f"{item.name}: 大小不符 {written}/{size}"
    os.replace(part, path)
    return True, f"{item.name}（{written / 1024 / 1024:.1f}MB）"


def _rpc_url(host_port: str) -> str:
    if "://" in host_port:
        scheme, rest = host_port.split("://", 1)
        host, _, path = rest.partition("/")
        return f"{scheme.lower()}://{host}/{path or 'jsonrpc'}"
    return f"http://{host_port}/jsonrpc"


async def _aria2_submit(
    driver: CloudDrive, items: list[_Item], cfg: DownloadSettings, log: LogFn
) -> list[str]:
    if not cfg.aria2_host_port:
        return ["❌ aria2 模式未配置 RPC 地址"]
    by_fid, cookie_str = await _resolve_links(driver, items)
    ua = getattr(driver, "UA", "Mozilla/5.0")
    url = _rpc_url(cfg.aria2_host_port)
    lines: list[str] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for item in items:
            row = by_fid.get(item.fid)
            if not row:
                lines.append(f"❌ 取直链失败: {item.name}")
                continue
            params: list = [
                [row["download_url"]],
                {
                    "header": [f"Cookie: {cookie_str}", f"User-Agent: {ua}"],
                    "out": item.local_path.name,
                    "dir": str(item.local_path.parent),
                    "pause": str(cfg.aria2_pause).lower(),
                },
            ]
            if cfg.aria2_secret:
                params.insert(0, f"token:{cfg.aria2_secret}")
            payload = {"jsonrpc": "2.0", "id": "xiao-pan", "method": "aria2.addUri", "params": params}
            try:
                result = (await client.post(url, json=payload)).json()
            except Exception as exc:  # noqa: BLE001
                lines.append(f"❌ aria2 连接失败: {exc}")
                break
            if result.get("result"):
                log("info", f"📥 aria2 已投递 {item.name}")
                lines.append(f"✅ aria2 已投递 {item.name}")
            else:
                lines.append(f"❌ aria2 {item.name}: {result.get('error')}")
    return lines


async def _emby_refresh(cfg: DownloadSettings, log: LogFn) -> None:
    if not (cfg.emby_url and cfg.emby_token):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{cfg.emby_url.rstrip('/')}/emby/Library/Refresh",
                params={"api_key": cfg.emby_token},
            )
        log("info", f"Emby 媒体库刷新: HTTP {resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        log("warn", f"Emby 刷新失败: {exc}")
