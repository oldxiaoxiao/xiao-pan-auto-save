"""本地下载：转存成功后把新文件落到本地磁盘。

两种模式（协议对齐原项目 aria2 插件）：
- builtin：服务端 httpx 流式下载，.part 临时文件 + 完成后改名，目标是「验证过的完整文件」则跳过，
  按配置并发数并行；
- aria2：JSON-RPC addUri 投递直链任务（带网盘 Cookie/UA 头），支持 pause 挂起；
  投递前用同一份判据做预检，完整文件不重下，残骸/占位一律重投。
「验证过的完整文件」= 常规文件 + 账本已知大小精确相符 + 无 <name>.aria2 控制文件 + 非稀疏预分配
（见 download_history._file_check，全项目唯一一份 stat 解读）。
下载成功后如配置了 Emby，触发一次媒体库刷新。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import PROXY
from ..core.download_registry import registry
from ..core.engine import SavedFile
from ..core.logstream import LogFn
from ..drivers.base import CloudDrive, DriveError
from . import download_history as history

DEFAULT_DOWNLOAD_DIR = str(Path(__file__).resolve().parent.parent.parent / "data" / "downloads")


@dataclass
class DownloadSettings:
    mode: str = "builtin"  # builtin | aria2
    dir: str = DEFAULT_DOWNLOAD_DIR
    concurrency: int = 2
    history_retention: str = "days_90"  # days_30 | days_90 | days_180 | forever
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
            # 下载根目录出口即归一化为绝对**字面**路径：相对 dir（如 data/downloads）会原样
            # 进账本 dest_path，file_state 校验与重下全凭服务端 CWD 碰运气（2026-10-05 活体发现）。
            # 必须用 abspath 而非 Path.resolve()：resolve 展开软链会改写容器共享挂载路径
            # （/tmp → /private/tmp），真机 aria2 直接 errorCode 18（见 b1d156b）。
            dir=os.path.abspath(str(raw.get("dir") or DEFAULT_DOWNLOAD_DIR)),
            concurrency=max(1, int(raw.get("concurrency") or 2)),
            history_retention=str(raw.get("history_retention") or "days_90"),
            aria2_host_port=str(aria2.get("host_port") or ""),
            aria2_secret=str(aria2.get("secret") or ""),
            aria2_pause=bool(aria2.get("pause")),
            emby_url=str(emby.get("url") or ""),
            emby_token=str(emby.get("token") or ""),
        )


@dataclass
class DownloadItem:
    fid: str
    name: str
    size: int
    local_path: Path


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 同路径在途下载守卫：内置下载器正在落盘 / aria2 正在投递的 dest_path（解析后）集合。
# 查库的 has_open_for_path 拦不住"取直链窗口"里的第二次点击——账本行要等取到直链才写入，
# 这层在进程内于登记 registry/写账本之前直接按路径拦截，检查+登记之间无 await，
# asyncio 单线程语义天然原子，无需加锁。仅覆盖单 uvicorn worker（本项目默认单进程），跨进程并发不在范围内。
_inflight_paths: set[str] = set()


def _inflight_key(path: str | Path) -> str:
    return str(Path(path).resolve())


def is_downloading(path: str) -> bool:
    """该目标路径是否已有下载在途（内置落盘中或 aria2 投递窗口内；重下路由用，不暴露集合本身）。"""
    return _inflight_key(path) in _inflight_paths


def safe_name(name: str) -> str:
    cleaned = _UNSAFE.sub("_", (name or "").strip()).lstrip(".")
    return cleaned[:200] or "_"


def _history_start(log, *, source: str, ref_id: str, item: DownloadItem, size: int, task_id, taskname,
                   account_id, driver_key: str) -> None:
    try:
        history.start(
            source=source, ref_id=ref_id, task_id=task_id, taskname=taskname, filename=item.name,
            dest_path=str(item.local_path), size_total=size, fid=item.fid,
            driver_key=driver_key, account_id=account_id,
        )
    except Exception as exc:  # noqa: BLE001 账本是旁路观测，绝不中断下载
        log("warn", f"下载账本写入失败（不影响下载）：{exc}")


def _history_finish(log, *, source: str, ref_id: str, ok: bool, fallback_name: str) -> None:
    try:
        job = registry.get(ref_id)
        # job 可能为 None（终态被内存注册表淘汰），此时用下载结果兜底，账本不丢终态。
        status = job.status if job and job.status in history.TERMINAL else ("done" if ok else "failed")
        # size 传 None 而不是 0：finish 会跳过这两个字段，保住 start 落的真实 size_total，
        # 不让淘汰场景把账本体积抹平成 0。
        done = job.done if job else None
        total = job.total if job else None
        error = job.error if job else ("" if ok else fallback_name)
        history.finish(ref_id, source=source, status=status, size_done=done, size_total=total, error=error)
    except Exception as exc:  # noqa: BLE001
        log("warn", f"下载账本终态写入失败（不影响下载）：{exc}")


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
) -> list[DownloadItem]:
    """待下载清单：文件直接入列；目录在开启递归时深度遍历（本地镜像子结构）。"""
    items: list[DownloadItem] = []
    for f in saved:
        local = resolve_local(f.dest_path, cfg, override)
        if not f.is_dir:
            items.append(DownloadItem(f.new_fid, Path(f.dest_path).name, 0, local))
        elif download_subdir:
            items.extend(await _walk_dir(driver, f.new_fid, local))
    return items


async def _walk_dir(driver: CloudDrive, fid: str, local_dir: Path) -> list[DownloadItem]:
    out: list[DownloadItem] = []
    try:
        children = await driver.list_dir_children(fid)
    except DriveError:
        return out
    for c in children:
        child = local_dir / safe_name(c.name)
        if c.is_dir:
            out.extend(await _walk_dir(driver, c.fid, child))
        else:
            out.append(DownloadItem(c.fid, c.name, c.size, child))
    return out


async def download_items(
    driver: CloudDrive, items: list[DownloadItem], cfg: DownloadSettings, *, log: LogFn,
    task_id: int | None = None, taskname: str = "", account_id: int | None = None, driver_key: str = "",
) -> list[str]:
    """执行已解析的下载清单：分派内置/aria2、写账本、成功后刷新 Emby。"""
    if cfg.mode == "aria2" and not await aria2_reachable(cfg):
        log("warn", "⚠️ aria2 不可达，自动改用内置下载器（保证下载不中断）")
        cfg = _as_builtin(cfg)
    if cfg.mode == "aria2":
        lines = await _aria2_submit(
            driver, items, cfg, log, task_id=task_id, taskname=taskname, account_id=account_id, driver_key=driver_key
        )
    else:
        lines = await _builtin_download(
            driver, items, cfg, log, task_id=task_id, taskname=taskname, account_id=account_id, driver_key=driver_key
        )
    if any(line.startswith("✅") for line in lines):
        await _emby_refresh(cfg, log)
    return lines


async def download_task_files(
    driver: CloudDrive,
    saved: list[SavedFile],
    cfg: DownloadSettings,
    *,
    download_subdir: bool = False,
    savepath_override: str = "",
    log: LogFn | None = None,
    task_id: int | None = None,
    taskname: str = "",
    account_id: int | None = None,
) -> list[str]:
    """转存成功后的文件落本地：解析清单 → 执行下载。返回摘要行（供通知聚合）。"""
    log = log or (lambda level, msg: None)
    if not saved:
        return []
    items = await collect_files(driver, saved, cfg, savepath_override, download_subdir)
    if not items:
        return []
    return await download_items(
        driver, items, cfg, log=log, task_id=task_id, taskname=taskname,
        account_id=account_id, driver_key=driver.key,
    )


def _as_builtin(cfg: DownloadSettings) -> DownloadSettings:
    from dataclasses import replace

    return replace(cfg, mode="builtin")


async def aria2_reachable(cfg: DownloadSettings) -> bool:
    """探测 aria2 RPC 是否可用（getVersion 有 result）。未配地址/异常一律 False。"""
    if not cfg.aria2_host_port:
        return False
    url = _rpc_url(cfg.aria2_host_port)
    token = [f"token:{cfg.aria2_secret}"] if cfg.aria2_secret else []
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                url, json={"jsonrpc": "2.0", "id": "ping", "method": "aria2.getVersion", "params": token}
            )
        return bool(resp.json().get("result"))
    except Exception:  # noqa: BLE001 任何异常都视为不可达，交由内置下载器兜底
        return False


async def _resolve_links(driver: CloudDrive, items: list[DownloadItem]) -> tuple[dict[str, dict], str]:
    rows, cookie_str = await driver.get_download_urls([i.fid for i in items])
    return {r["fid"]: r for r in rows if r.get("download_url")}, cookie_str


async def _builtin_download(
    driver: CloudDrive, items: list[DownloadItem], cfg: DownloadSettings, log: LogFn, *, task_id=None, taskname="",
    account_id=None, driver_key=""
) -> list[str]:
    by_fid, cookie_str = await _resolve_links(driver, items)
    ua = getattr(driver, "UA", "Mozilla/5.0")
    sem = asyncio.Semaphore(cfg.concurrency)

    async def one(item: DownloadItem) -> str:
        row = by_fid.get(item.fid)
        if not row:
            return f"❌ 取直链失败: {item.name}"
        # 在途守卫必须在 registry.create / 账本 start 之前：被拒的尝试根本没有开始下载，
        # 不该留下一条永远等不到终态的账本行。检查与登记之间没有 await，单线程下原子。
        key = _inflight_key(item.local_path)
        if key in _inflight_paths:
            return f"❌ {item.name}: 同一文件已有下载在途"
        _inflight_paths.add(key)
        try:
            size = int(row.get("size") or item.size or 0)
            job_id = registry.create(
                task_id=task_id, taskname=taskname, filename=item.name,
                dest_path=str(item.local_path), total=size,
            )
            _history_start(log, source="builtin", ref_id=job_id, item=item, size=size, task_id=task_id,
                           taskname=taskname, account_id=account_id, driver_key=driver_key)
            async with sem:
                try:
                    ok, msg = await _fetch_one(row, item, cookie_str, ua, job_id=job_id)
                except Exception as exc:  # noqa: BLE001
                    registry.update(job_id, status="failed", error=str(exc))
                    ok, msg = False, f"{item.name}: {exc}"
            _history_finish(log, source="builtin", ref_id=job_id, ok=ok, fallback_name=msg)
            log("info" if ok else "warn", f"📥 {msg}")
            return f"{'✅' if ok else '❌'} {msg}"
        finally:
            _inflight_paths.discard(key)

    return list(await asyncio.gather(*(one(i) for i in items)))


async def _fetch_one(row: dict, item: DownloadItem, cookie_str: str, ua: str, *, job_id: str | None = None) -> tuple[bool, str]:
    path = item.local_path
    path.parent.mkdir(parents=True, exist_ok=True)
    size = int(row.get("size") or item.size or 0)
    # 「目标已经是验证过的完整文件」全项目只有一份判据：download_history._file_check。
    # 这里以前自己写 `path.exists() and size and st_size == size`（第三处变体），只看大小就把
    # aria2 --file-allocation 预分配出来的整片占位读成"已存在"而跳过（评审 Critical 1）；
    # 大小未知的行也曾在两种模式间分叉（内置重下、aria2 假跳过，Important 3）。
    state, matches, _st = history._file_check(str(path), size)
    if state == "ok" and matches:
        if job_id:
            registry.update(job_id, status="skipped")
        return True, f"跳过（已存在）{item.name}"
    part = path.with_name(path.name + ".part")
    headers = {"user-agent": ua}
    if cookie_str:
        headers["cookie"] = cookie_str
    started = time.monotonic()
    last_tick = started
    written = 0
    # read=60s 空闲上限：卡住的流最迟 60s 后抛 ReadTimeout 走失败路径，stop() 也能及时响应；
    # 免费盘分片间隔远小于 60s，不影响正常长下载（不加总超时）。
    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10, read=60), follow_redirects=True) as client:
        async with client.stream("GET", row["download_url"], headers=headers) as resp:
            if resp.status_code != 200:
                if job_id:
                    registry.update(job_id, status="failed", error=f"HTTP {resp.status_code}")
                return False, f"{item.name}: HTTP {resp.status_code}"
            with part.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 16):
                    if job_id and registry.cancel_requested(job_id):
                        fh.close()
                        part.unlink(missing_ok=True)
                        registry.update(job_id, status="stopped", error="已停止")
                        return False, f"{item.name}: 已停止"
                    fh.write(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if job_id and now - last_tick >= 0.25:
                        last_tick = now
                        speed = written / max(now - started, 1e-6)
                        registry.update(job_id, done=written, speed=speed, status="downloading")
    if size and written != size:
        part.unlink(missing_ok=True)
        if job_id:
            registry.update(job_id, status="failed", error=f"大小不符 {written}/{size}")
        return False, f"{item.name}: 大小不符 {written}/{size}"
    os.replace(part, path)
    if job_id:
        registry.update(job_id, done=written, total=written or size, status="done")
    return True, f"{item.name}（{written / 1024 / 1024:.1f}MB）"


def _rpc_url(host_port: str) -> str:
    if "://" in host_port:
        scheme, rest = host_port.split("://", 1)
        host, _, path = rest.partition("/")
        return f"{scheme.lower()}://{host}/{path or 'jsonrpc'}"
    return f"http://{host_port}/jsonrpc"


async def aria2_rpc(cfg: DownloadSettings, method: str, *params) -> dict:
    """向 aria2 RPC 发一条 JSON-RPC 指令（带 token 前缀），返回解析后的 json；异常向上抛。"""
    url = _rpc_url(cfg.aria2_host_port)
    token = [f"token:{cfg.aria2_secret}"] if cfg.aria2_secret else []
    payload = {"jsonrpc": "2.0", "id": "ctl", "method": method, "params": token + list(params)}
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.post(url, json=payload)
    return resp.json()


async def aria2_status(cfg: DownloadSettings) -> list[dict]:
    """查询 aria2 正在/排队下载，映射成与内置一致的 job 结构。

    仅 aria2 模式且配了 RPC 时查询；未配置或不可达一律返回 []（不影响内置任务展示）。
    aria2 不携带所属任务，taskname 留空；下载完成后离开 active/waiting 即不再出现。
    """
    if cfg.mode != "aria2" or not cfg.aria2_host_port:
        return []
    url = _rpc_url(cfg.aria2_host_port)
    token = [f"token:{cfg.aria2_secret}"] if cfg.aria2_secret else []
    keys = ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "files"]
    out: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=5) as client:
                # tellWaiting 窗口取 200：排队深过窗口的 gid 对 reconcile 不可见，会被 24h 规则误判失败——盲区宁大勿漏
            for method, extra in (("aria2.tellActive", []), ("aria2.tellWaiting", [0, 200])):
                params = token + extra + [keys]
                resp = await client.post(
                    url, json={"jsonrpc": "2.0", "id": "st", "method": method, "params": params}
                )
                for st in resp.json().get("result") or []:
                    files = st.get("files") or []
                    path = files[0].get("path", "") if files else ""
                    raw = st.get("status")
                    status = {"active": "downloading", "paused": "paused"}.get(raw, "queued")
                    out.append(
                        {
                            "id": st.get("gid") or path,
                            "task_id": None,
                            "taskname": "",
                            "filename": Path(path).name or "aria2",
                            "dest_path": path,
                            "total": int(st.get("totalLength") or 0),
                            "done": int(st.get("completedLength") or 0),
                            "speed": float(st.get("downloadSpeed") or 0),
                            "status": status,
                            "error": "",
                            "started_at": 0.0,
                            "updated_at": time.time(),
                            "source": "aria2",
                        }
                    )
    except Exception:  # noqa: BLE001 aria2 不可达时静默降级
        return []
    return out


async def _aria2_submit(
    driver: CloudDrive, items: list[DownloadItem], cfg: DownloadSettings, log: LogFn, *,
    task_id=None, taskname="", account_id=None, driver_key=""
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
            # 在途守卫与内置下载器同款：连点两次「重下」若都落在"取直链→addUri"窗口里，
            # daemon 会收下两份整片（aria2 撞名不去重、自动改名 .1.mkv，2026-10-05 活体实证）。
            # 检查与登记之间无 await，单线程下原子；被拦的尝试不投递、不落账本。
            key = _inflight_key(item.local_path)
            if key in _inflight_paths:
                lines.append(f"❌ {item.name}: 同一文件已有下载在途")
                continue
            _inflight_paths.add(key)
            try:
                # 投递前到位预检：目标是「验证过的完整文件」就直接跳过，不投给 daemon。缺这层的活体
                # 翻车（2026-10-05）：对已下完的 S01E194.mkv 点重下，aria2 撞名不改写而是自动改名
                # .1.mkv 重下整片 3.5GB，新行 dest_path 却仍记原路径，对账把原文件误判成本次下载的结果。
                # 判据复用 download_history._file_check 这唯一一处 stat 解读（内置 _fetch_one、对账兜底、
                # UI 的 file_state 用的是同一份，全项目没有第二变体）：常规文件 + 已知大小精确相符 +
                # 没有 <name>.aria2 控制文件 + 不是预分配稀疏占位，四条齐了才叫到位；大小未知（0）
                # 一律"没验证过"→ 照常投递（评审 Critical 1/Important 3：只看大小会把 aria2 失败后
                # 留下的整片占位读成已到位，于是每次重下都同样跳过，这个文件在 UI 里永远修不好）。
                # 顺序选「在途守卫在前、预检在后」：aria2 会预分配整片大小的占位文件，投递窗口内
                # 并发进来的第二次点击若先做预检，会把没下完的占位误判成完整而假跳过；先过守卫，
                # 同路径在途动作一律吃「已有下载在途」，预检只面对无在途的终态文件。
                expected = int(row.get("size") or item.size or 0)
                state, matches, _st = history._file_check(str(item.local_path), expected)
                if state == "ok" and matches:
                    # 账本按内置路径同款落一行终态 skipped。内置行以 registry job_id、aria2 行以 daemon
                    # gid 为键，而跳过的尝试从未投递、两头都没有 id——按 DownloadRegistry.create
                    # 的做法铸一个 uuid4 hex 前 12 位作 ref_id；终态行不进 open_records，
                    # 这个非 gid 的 ref 永远不会被 reconcile 拿去问 tellStatus。
                    ref_id = uuid.uuid4().hex[:12]
                    _history_start(log, source="aria2", ref_id=ref_id, item=item, size=expected, task_id=task_id,
                                   taskname=taskname, account_id=account_id, driver_key=driver_key)
                    # 不传 size_done/size_total：与内置 skipped 行同形（size_total=start 落的预期大小，
                    # size_done 保持默认 0——下载从未开始）。
                    history.finish(ref_id, source="aria2", status="skipped")
                    log("info", f"📥 跳过（已存在）{item.name}")  # 与内置路径的运行日志同款
                    lines.append(f"✅ 跳过（已存在）{item.name}")
                    continue
                # aria2 不会自建缺失目录，投递前先建好目标目录（与内置下载器一致）。
                # dir 必须用绝对路径：aria2 常在容器内运行，相对路径会按容器 CWD 解析，
                # 导致文件落进容器而非宿主机挂载目录（内置下载器跑在宿主机不受影响）。
                # 用 abspath 而非 resolve：resolve 会展开符号链接——macOS 宿主的 /tmp 实为
                # /private/tmp 的软链，容器里只挂载字面 /tmp/... 路径，改写后真机直接
                # errorCode 18 失败（2026-10-05 活体验证发现）；abspath 只补绝对、保留字面挂载路径。
                dest_dir = Path(os.path.abspath(item.local_path.parent))
                dest_dir.mkdir(parents=True, exist_ok=True)
                params: list = [
                    [row["download_url"]],
                    {
                        "header": [f"Cookie: {cookie_str}", f"User-Agent: {ua}"],
                        "out": item.local_path.name,
                        "dir": str(dest_dir),
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
                gid = result.get("result")
                if gid:
                    log("info", f"📥 aria2 已投递 {item.name}")
                    _history_start(log, source="aria2", ref_id=str(gid), item=item,
                                   size=int(row.get("size") or item.size or 0), task_id=task_id,
                                   taskname=taskname, account_id=account_id, driver_key=driver_key)
                    lines.append(f"✅ aria2 已投递 {item.name}")
                else:
                    lines.append(f"❌ aria2 {item.name}: {result.get('error')}")
            finally:
                # 投递 RPC 返回、queued 账本行落下即释放：之后的重复点击由 has_open_for_path
                # （DB 层的 queued 行）接管，这一层只负责账本行出现之前的窗口。
                _inflight_paths.discard(key)
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


def _account_for(rec: dict):
    """重下用账号：优先记录里的 account_id，失效则按 driver_key 选可用主账号（语义同 routes_files._primary_account）。"""
    from sqlmodel import select

    from ..database import session_scope
    from ..models import Account

    with session_scope() as session:
        acc = session.get(Account, int(rec["account_id"])) if rec.get("account_id") else None
        if acc is not None and acc.enabled:
            return acc
        accs = session.exec(
            select(Account)
            .where(Account.enabled, Account.driver_key == rec["driver_key"])
            .order_by(Account.sort_order, Account.id)
        ).all()
        return next((a for a in accs if a.can_save), accs[0] if accs else None)


async def retry_record(rec: dict, cfg: DownloadSettings, *, log: LogFn) -> None:
    """按账本记录重下单个文件：目标路径原样保留，直链重新获取。

    不走 resolve_local：下载根目录/覆盖路径可能在首次下载后改过，重下必须打回原 dest_path。
    账本仍由 download_items 内的既有写入点落一条新行（一次尝试一行），不改写旧记录。
    """
    from ..drivers import get_driver_class

    cls = get_driver_class(rec.get("driver_key") or "")
    if cls is None or not cls.supported:
        log("error", f"《{rec.get('taskname') or ''}》重下失败：{rec.get('driver_key')} 驱动不可用")
        return
    acc = _account_for(rec)
    if acc is None:
        log("error", f"《{rec.get('taskname') or ''}》重下失败：没有可用的 {rec.get('driver_key')} 账号")
        return
    driver = cls(cookie=acc.cookie, proxy=PROXY, index=acc.sort_order)
    if not driver.has("download"):
        log("error", f"《{rec.get('taskname') or ''}》重下失败：{driver.name} 不支持下载")
        return
    item = DownloadItem(
        fid=rec["fid"], name=rec["filename"], size=int(rec["size_total"] or 0), local_path=Path(rec["dest_path"])
    )
    # 与首下路径（task_service._download_for_task）同等的兜底：retry_record 由路由层 create_task
    # 裸调度，取直链/驱动异常若不上抛进日志，只会烂在 uvicorn stderr 的
    # "Task exception was never retrieved" 里 —— 账本与日志 tab 两头无痕，而 UI 已承诺"重下已开始"。
    # 典型炸点在取直链阶段（_history_start 之前），重下失败不新增账本行；
    # 极小概率的半途异常若留下非终态行，也由 reconcile 收口，不留悬挂。
    try:
        lines = await download_items(
            driver, [item], cfg, log=log, task_id=rec.get("task_id"), taskname=rec.get("taskname") or "",
            account_id=acc.id, driver_key=rec["driver_key"],
        )
    except Exception as exc:  # noqa: BLE001 后台重下异常不允许静默丢失，必须落日志
        log("error", f"《{rec.get('taskname') or ''}》重下异常：{exc}")
        return
    ok = any(x.startswith("✅") for x in lines)
    log("info" if ok else "warn", f"🔁 重下 {rec['filename']}：{lines or '无结果'}")
