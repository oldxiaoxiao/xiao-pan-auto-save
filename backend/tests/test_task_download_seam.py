"""转存→下载账本的字段接线集成测试。

钉住真实调用链：task_service.run_tasks → _run_tasks_inner（updated + auto_download）→
_download_for_task → download_service.download_task_files → _builtin_download →
download_history.start/finish。只 stub 两处天然需要网络/云盘的点：

- run_update_task（转存检测段，走真机需要网盘）：直接喂一个 status="updated" 的结果，
  携带真实形状的 SavedFile 清单——这是生产里 _download_for_task 唯一的入参来源。
- DlDriver（假驱动，实现 download 能力，返回假直链 + cookie）+ stub _fetch_one（落盘字节）。

其余全按生产路径跑：账号选取（_pick_account 读真库 Account）、驱动构造、路径解析
（resolve_local 的镜像 / 平铺两种语义）、账本写入点都在被测范围内。断言只看回读的
DownloadRecord 行，且一律按 task_id 作用域，不依赖表级计数或用例顺序。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import delete, select

from backend.core.engine import SavedFile, TaskRunResult
from backend.database import session_scope
from backend.drivers.base import FsItem
from backend.models import Account, DownloadRecord, Task
from backend.services import download_service as dl
from backend.services import task_service
from backend.tests.test_download import DlDriver


class _DirDlDriver(DlDriver):
    """带固定目录子项的 DlDriver：走 download_subdir 递归段（list_dir_children）。"""

    _CHILDREN = {
        "dirD": [
            FsItem(fid="ca", name="a.mp4", size=8),
            FsItem(fid="cb", name="b.mp4", size=9),
        ],
    }

    def __init__(self, **kw):
        super().__init__(children=self._CHILDREN, **kw)


@pytest.fixture(autouse=True)
def _clean_db():
    def _wipe():
        with session_scope() as s:
            s.exec(delete(Task))
            s.exec(delete(Account))
            s.exec(delete(DownloadRecord))

    _wipe()
    yield
    _wipe()


def _sf(fid: str, name: str, dest: str, is_dir: bool = False) -> SavedFile:
    return SavedFile(share_name=name, final_name=name, new_fid=fid, dest_path=dest, is_dir=is_dir)


def _seed_task(driver_key: str = "fake", **task_kw) -> tuple[Account, Task]:
    """落库一个可用账号 + auto_download 任务，返回 (account, task)。"""
    with session_scope() as s:
        acc = Account(driver_key=driver_key, cookie="CK", enabled=True, can_save=True, name="主号")
        s.add(acc)
        s.commit()
        s.refresh(acc)
        task = Task(
            taskname=task_kw.pop("taskname", "剧集"),
            shareurl="https://fake.example/s/1",
            savepath="/动漫/剧",
            account_id=acc.id,
            auto_download=True,
            **task_kw,
        )
        s.add(task)
        s.commit()
        s.refresh(task)
        return acc, task


def _patch_chain(monkeypatch, tmp_path: Path, results: dict[str, TaskRunResult], driver_cls) -> list:
    """把 run_tasks 上游需要网络/配置的点全部 stub，保留下载→账本段为真实代码。返回 pushed。"""
    import backend.api.deps as deps

    settings = {
        "download": {"mode": "builtin", "dir": str(tmp_path / "down"), "concurrency": 2},
        "magic_regex": {},
        "push_config": {},
        "notify_enabled": True,
    }
    monkeypatch.setattr(deps, "all_settings", lambda: settings)
    monkeypatch.setattr(task_service, "route_driver", lambda url: driver_cls)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return results[spec.taskname]

    monkeypatch.setattr(task_service, "run_update_task", fake_run_update)

    pushed: list[tuple[str, str]] = []

    async def fake_push(title, content, push_config, settings_, log):
        pushed.append((title, content))

    monkeypatch.setattr(task_service, "_push", fake_push)

    # 下载层 stub：Emby 刷新不发；取直链后写盘用假 _fetch_one（成功路径直接返回 True，
    # _history_finish 的 fallback 会把账本收口为 done，无需真碰 registry 进度）。
    async def _noop():
        return None

    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())
    return pushed


def _rows(task_id: int) -> list[DownloadRecord]:
    with session_scope() as s:
        return list(
            s.exec(
                select(DownloadRecord).where(DownloadRecord.task_id == task_id).order_by(DownloadRecord.id)
            ).all()
        )


@pytest.mark.asyncio
async def test_mirror_and_override_pin_ledger_fields(monkeypatch, tmp_path):
    """一次 run_tasks 驱动两个任务：镜像（download_savepath 空）与平铺（覆盖路径），
    逐字段核对账本行，验证 task_id/taskname/account_id/driver_key/fid/status/dest_path 接线。"""
    acc_a, task_a = _seed_task(taskname="镜像剧", download_savepath="")
    acc_b, task_b = _seed_task(taskname="平铺剧", download_savepath="剧集专区")
    # task_b 用另一个账号，验证 account_id 按所选账号落库
    assert acc_b.id != acc_a.id

    results = {
        "镜像剧": TaskRunResult(status="updated", files=[_sf("f1", "01.mp4", "/动漫/剧/01.mp4")]),
        "平铺剧": TaskRunResult(status="updated", files=[_sf("f2", "02.mp4", "/动漫/剧/02.mp4")]),
    }
    _patch_chain(monkeypatch, tmp_path, results, DlDriver)

    async def ok_fetch(row, item, cookie_str, ua, *, job_id=None):
        return True, f"{item.name}（0.0MB）"

    monkeypatch.setattr(dl, "_fetch_one", ok_fetch)

    summary = await task_service.run_tasks([task_a.id, task_b.id], trigger="manual")
    assert summary["updated"] == 2 and summary["failed"] == 0

    rows_a = _rows(task_a.id)
    assert len(rows_a) == 1  # 恰好每个下载文件一行（start+finish 同一条）
    ra = rows_a[0]
    assert ra.taskname == "镜像剧"
    assert ra.account_id == acc_a.id
    assert ra.driver_key == DlDriver.key == "fake"
    assert ra.fid == "f1"
    assert ra.status == "done" and ra.source == "builtin"
    # 镜像语义：下载根/网盘目录逐层还原
    assert Path(ra.dest_path) == tmp_path / "down" / "动漫" / "剧" / "01.mp4"

    rows_b = _rows(task_b.id)
    assert len(rows_b) == 1
    rb = rows_b[0]
    assert rb.taskname == "平铺剧"
    assert rb.account_id == acc_b.id
    assert rb.status == "done"
    # 平铺语义：download_savepath 覆盖 → 下载根/覆盖路径/文件名，网盘目录层级被丢弃
    assert Path(rb.dest_path) == tmp_path / "down" / "剧集专区" / "02.mp4"
    assert "动漫" not in rb.dest_path


@pytest.mark.asyncio
async def test_recursive_subdir_rows_per_child(monkeypatch, tmp_path):
    """download_subdir=True 且转存项是目录：递归展开后，每个子文件各落一行，
    task_id/taskname/account_id 仍归属该任务，dest_path 落在目录镜像之下。"""
    acc, task = _seed_task(taskname="目录剧", download_subdir=True, download_savepath="")
    results = {
        "目录剧": TaskRunResult(status="updated", files=[_sf("dirD", "剧集", "/动漫/剧/剧集", is_dir=True)]),
    }
    _patch_chain(monkeypatch, tmp_path, results, _DirDlDriver)

    async def ok_fetch(row, item, cookie_str, ua, *, job_id=None):
        return True, f"{item.name}（0.0MB）"

    monkeypatch.setattr(dl, "_fetch_one", ok_fetch)

    summary = await task_service.run_tasks([task.id], trigger="manual")
    assert summary["updated"] == 1

    rows = _rows(task.id)
    assert len(rows) == 2  # 目录下 a.mp4 + b.mp4，一项一行
    by_fid = {r.fid: r for r in rows}
    assert set(by_fid) == {"ca", "cb"}
    for r in rows:
        assert r.taskname == "目录剧"
        assert r.account_id == acc.id
        assert r.driver_key == "fake"
        assert r.status == "done"
    assert Path(by_fid["ca"].dest_path) == tmp_path / "down" / "动漫" / "剧" / "剧集" / "a.mp4"
    assert Path(by_fid["cb"].dest_path) == tmp_path / "down" / "动漫" / "剧" / "剧集" / "b.mp4"


@pytest.mark.asyncio
async def test_download_failure_marks_failed_but_run_succeeds(monkeypatch, tmp_path):
    """下载抛错的形状：坏文件收口为 failed 并带错误文本，好文件仍 done；
    且下载失败不得拖垮转存结果——任务仍计为 updated，通知里既有追更成功也有下载失败明细。"""
    acc, task = _seed_task(taskname="半坏剧")
    results = {
        "半坏剧": TaskRunResult(
            status="updated",
            files=[_sf("good", "好.mp4", "/动漫/剧/好.mp4"), _sf("bad", "坏.mp4", "/动漫/剧/坏.mp4")],
        ),
    }
    pushed = _patch_chain(monkeypatch, tmp_path, results, DlDriver)

    async def boom_fetch(row, item, cookie_str, ua, *, job_id=None):
        if item.name == "坏.mp4":
            raise RuntimeError("直链 403")
        return True, f"{item.name}（0.0MB）"

    monkeypatch.setattr(dl, "_fetch_one", boom_fetch)

    summary = await task_service.run_tasks([task.id], trigger="manual")
    # 下载失败不影响转存计数：任务依旧 updated、failed 为 0
    assert summary["updated"] == 1 and summary["failed"] == 0

    rows = _rows(task.id)
    assert len(rows) == 2
    by_fid = {r.fid: r for r in rows}
    assert by_fid["good"].status == "done"
    failed = by_fid["bad"]
    assert failed.status == "failed"
    assert "直链 403" in failed.error
    assert failed.account_id == acc.id and failed.taskname == "半坏剧"

    # 转存/通知行为不变：既有添加追更的成功行，也带下载失败明细（1/2）
    content = pushed[0][1]
    assert "添加追更" in content
    assert "本地下载 1/2" in content
