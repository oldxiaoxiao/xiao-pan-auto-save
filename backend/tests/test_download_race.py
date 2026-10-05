"""同路径并发重下的竞态守卫回归：堵住"取直链窗口"里的第二个写者。

背景：重下路由的 has_open_for_path 查的是数据库，而第一次重下的账本行要等
_builtin_download.one() 取到直链之后才写入。两次点击若都落在这个窗口里，DB 检查双双放行，
两个内置写者并发写同一个 <name>.part —— 交错字节、双重改名，把文件写坏。

本用例用真实的 download_items/_fetch_one 代码路径并发跑两次同 dest_path 的下载，
只把 HTTP 层换成"慢速分块"假流（块间 sleep(0) 强制两个写者在 async for 里真正交错），
以复现竞态并钉住守卫的期望行为：
1) 后到者被进程内守卫拦截，摘要行给出中文原因；
2) 被拦截的尝试不写账本（下载从未开始）；
3) 先到的下载完整落盘，字节不被第二个写者污染；
4) 守卫登记必须随下载结束释放，同路径稍后仍可正常再下载。
"""

from __future__ import annotations

import asyncio

from sqlmodel import select

from backend.core.download_registry import registry
from backend.database import session_scope
from backend.models import DownloadRecord
from backend.services import download_service as dl
from backend.tests.test_download import DlDriver, cfg

CHUNK = 100
N_CHUNKS = 10
SOURCE_LEN = CHUNK * N_CHUNKS


def payload(pad: bytes) -> bytes:
    """每个写者一份等长但可区分的内容，落盘后能看出是谁写的。"""
    return pad * SOURCE_LEN


class SlowResp:
    status_code = 200

    def __init__(self, pad: bytes):
        self._pad = pad

    async def aiter_bytes(self, _n):
        for _ in range(N_CHUNKS):
            await asyncio.sleep(0)  # 每块之间让出事件循环，逼两个写者交错进同一个 .part
            yield self._pad * CHUNK

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class SlowClient:
    """按实例顺序分配 pad：第 1 个构造的写者写 b"A"，第 2 个写 b"B"。"""

    n = 0

    def __init__(self, **kw):
        SlowClient.n += 1
        self._pad = bytes([64 + SlowClient.n])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, headers=None):
        return SlowResp(self._pad)


class SizedDriver(DlDriver):
    """复用仓库自带的假驱动，只把直链行的 size 改成假流的真实长度。"""

    async def get_download_urls(self, fids):
        rows = [
            {"fid": f, "file_name": f, "size": SOURCE_LEN, "download_url": f"http://dl/{f}"} for f in fids
        ]
        return rows, "DLCK=1"


async def _run_once(dest, task_id: int) -> list[str]:
    items = [dl.DownloadItem(fid="race-1", name="01.mkv", size=SOURCE_LEN, local_path=dest)]
    return await dl.download_items(
        SizedDriver(), items, cfg(dest.parent), log=lambda *a: None,
        task_id=task_id, taskname="竞态", account_id=None, driver_key="fake",
    )


def _rows(task_id: int) -> list[DownloadRecord]:
    with session_scope() as s:
        return list(s.exec(select(DownloadRecord).where(DownloadRecord.task_id == task_id)).all())


def _cleanup(task_ids: list[int]) -> None:
    with session_scope() as s:
        for r in s.exec(select(DownloadRecord).where(DownloadRecord.task_id.in_(task_ids))).all():
            registry.remove(r.ref_id)
            s.delete(r)


async def test_concurrent_same_dest_duplicate_is_rejected(tmp_path, monkeypatch):
    """并发第二次必须被进程内在途守卫拦下：不落账本、不碰第一个写者的字节。"""
    monkeypatch.setattr(dl.httpx, "AsyncClient", SlowClient)
    SlowClient.n = 0
    dest = tmp_path / "剧" / "01.mkv"
    task_id = 941
    try:
        lines_a, lines_b = await asyncio.gather(_run_once(dest, task_id), _run_once(dest, task_id))
        all_lines = lines_a + lines_b
        # 1) 后到者被守卫拦截，返回中文摘要行（守卫缺席时这里就是 RED 证据：两次都被放行）
        assert any("同一文件已有下载在途" in x and x.startswith("❌") for x in all_lines), all_lines
        # 2) 恰好一个成功
        assert sum(1 for x in all_lines if x.startswith("✅")) == 1, all_lines
        # 3) 被拒的尝试不写账本：本 task 只有真正开始的那一条
        #    （守卫缺席时会看到两条同 dest_path 的行，正是双写者的直接证据）
        rows = _rows(task_id)
        assert len(rows) == 1, [(r.ref_id, r.status) for r in rows]
        assert rows[0].status == "done"
        # 4) 先到的下载完整落盘：字节是第一个写者的载荷，未被第二个写者污染
        assert dest.read_bytes() == payload(b"A")
        assert not list(dest.parent.glob("*.part"))
    finally:
        _cleanup([task_id])


async def test_guard_releases_after_download_completes(tmp_path, monkeypatch):
    """守卫登记必须随下载结束释放：同路径串行重下不受影响（守卫不能把好路堵死、漏登记）。"""
    monkeypatch.setattr(dl.httpx, "AsyncClient", SlowClient)
    SlowClient.n = 0
    dest = tmp_path / "02.mkv"
    task_id = 942
    try:
        first = await _run_once(dest, task_id)
        assert first[0].startswith("✅"), first
        second = await _run_once(dest, task_id)  # 目标已存在且同大小 → 正常走「跳过」，而非在途拒绝
        assert any("跳过" in x for x in second), second
        assert not any("在途" in x for x in first + second), first + second
        assert len(_rows(task_id)) == 2  # 两次都真正开始过，各留一行
    finally:
        _cleanup([task_id])


# ---- aria2 模式同款在途守卫（2026-10-05 活体发现：连点两次「重下」下载出两份整片）----


def _aria2_race_client(posts: list):
    """假 RPC 客户端：记录每次 addUri 投递。被守卫拦下的尝试必须一次都没投出去。"""

    class C:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            await asyncio.sleep(0)  # 让另一个投递窗口穿插进来
            posts.append(json)

            class R:
                def json(self):
                    return {"result": "gid-aria2-race"}

            return R()

    return C


async def test_aria2_concurrent_same_dest_duplicate_is_rejected(tmp_path, monkeypatch):
    """aria2 模式下连点两次重下：第二次必须被进程内守卫拦下，不投递、不落账本。

    活体复盘：_inflight_paths 只在 _builtin_download.one() 里登记，aria2 提交的
    "取直链→addUri" 窗口全程无守卫，两次重试都放行，daemon 真下了两份 3.5GB 整片
    （aria2 撞名不但不去重还自动改名 .1.mkv）。守卫补齐后这里必须只投一条 addUri。
    """
    posts: list = []
    monkeypatch.setattr(dl.httpx, "AsyncClient", _aria2_race_client(posts))

    async def reachable(_c):
        return True

    monkeypatch.setattr(dl, "aria2_reachable", reachable)
    dest = tmp_path / "剧" / "S01E194.mkv"
    task_id = 943
    c = dl.DownloadSettings(
        mode="aria2", dir=str(tmp_path), aria2_host_port="http://127.0.0.1:6800", aria2_secret="sec"
    )
    items = [dl.DownloadItem(fid="aria2-race-1", name="S01E194.mkv", size=10, local_path=dest)]
    try:
        lines_a, lines_b = await asyncio.gather(
            dl.download_items(DlDriver(), items, c, log=lambda *a: None, task_id=task_id, driver_key="fake"),
            dl.download_items(DlDriver(), items, c, log=lambda *a: None, task_id=task_id, driver_key="fake"),
        )
        all_lines = lines_a + lines_b
        assert sum(1 for x in all_lines if x.startswith("✅")) == 1, all_lines
        assert any(x.startswith("❌") and "同一文件已有下载在途" in x for x in all_lines), all_lines
        # 被拒的尝试绝不投给 daemon：只有一条 addUri
        assert len(posts) == 1, posts
        # 被拒的尝试也不落账本：本 task 只有真正投递的那一条 queued
        rows = _rows(task_id)
        assert len(rows) == 1 and rows[0].status == "queued" and rows[0].source == "aria2"
        # 投递返回、账本落行后必须释放登记：后续点击由 queued 行（DB 层）接管
        assert dl._inflight_key(dest) not in dl._inflight_paths
    finally:
        _cleanup([task_id])
