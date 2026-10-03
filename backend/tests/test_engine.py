"""引擎测试：用内存 FakeDriver 验证差集/忽略扩展名/startfid/子目录两种模式/重命名。"""

from __future__ import annotations

import itertools

import pytest

from backend.core.engine import TaskSpec, run_update_task
from backend.drivers.base import CloudDrive, FsItem, SaveResult, ShareBanned, ShareRef


class FakeDriver(CloudDrive):
    key = "fake"
    name = "假驱动"
    share_domains = ["fake.example"]
    supported = True
    capability = {"rename", "delete", "account"}

    def __init__(self, share: dict[str, list[FsItem]], dirs: dict[str, list[FsItem]] | None = None, **kw):
        super().__init__(**kw)
        self.share = share
        self.dirs = dirs if dirs is not None else {"/": []}
        self.renamed: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self._fid = itertools.count(1000)

    def parse_share(self, url: str) -> ShareRef:
        return ShareRef(url=url, pwd_id="fakeid", extra={"path_fids": {"": "0"}})

    async def list_share(self, ref: ShareRef, path: str = "") -> list[FsItem]:
        if path == "BAN":
            raise ShareBanned("分享已经失效啦")
        return self.share.get(path, [])

    async def list_dir(self, path: str) -> list[FsItem]:
        return list(self.dirs.get(path, []))

    async def ensure_dir(self, path: str) -> str:
        self.dirs.setdefault(path, [])
        return "dirfid"

    async def save(self, items: list[FsItem], dest_path: str, ref: ShareRef | None = None) -> SaveResult:
        saved = []
        for i in items:
            new = FsItem(fid=str(next(self._fid)), name=i.name, is_dir=i.is_dir, mtime=i.mtime)
            self.dirs[dest_path].append(new)
            saved.append(new)
        return SaveResult(ok=True, saved=saved)

    async def rename(self, item_or_fid, new_name: str) -> None:
        fid = item_or_fid if isinstance(item_or_fid, str) else item_or_fid.fid
        for items in self.dirs.values():
            for i in items:
                if i.fid == fid:
                    self.renamed.append((i.name, new_name))
                    i.name = new_name
        return None

    async def delete_items(self, items: list[FsItem], purge: bool = True) -> None:
        for path, existing in self.dirs.items():
            self.dirs[path] = [i for i in existing if i.fid not in {x.fid for x in items}]
        self.deleted.extend(i.name for i in items)


def f(fid: str, name: str, token: str = "", mtime: float = 0) -> FsItem:
    return FsItem(fid=fid, name=name, token=token, mtime=mtime)


def d(fid: str, name: str) -> FsItem:
    return FsItem(fid=fid, name=name, is_dir=True)


def spec(**kw) -> TaskSpec:
    base = dict(taskname="测试剧", shareurl="https://fake.example/s/abc", savepath="/动漫/测试剧")
    base.update(kw)
    return TaskSpec(**base)


@pytest.mark.asyncio
async def test_first_run_saves_all():
    drv = FakeDriver({"": [f("1", "01.mp4", "t1"), f("2", "02.mp4", "t2")]})
    res = await run_update_task(drv, spec())
    assert res.status == "updated"
    assert len(res.files) == 2
    assert res.files[0].dest_path == "/动漫/测试剧/01.mp4"


@pytest.mark.asyncio
async def test_second_run_no_changes():
    share = [f("1", "01.mp4", "t1")]
    drv = FakeDriver({"": share})
    await run_update_task(drv, spec())
    res2 = await run_update_task(drv, spec())
    assert res2.status == "no_changes"
    assert res2.files == []


@pytest.mark.asyncio
async def test_diff_only_new_files():
    drv = FakeDriver(
        {"": [f("1", "01.mp4"), f("2", "02.mp4"), f("3", "03.mp4")]},
        dirs={"/动漫/测试剧": [f("x", "01.mp4")]},
    )
    res = await run_update_task(drv, spec())
    assert {i.share_name for i in res.files} == {"02.mp4", "03.mp4"}


@pytest.mark.asyncio
async def test_ignore_extension():
    drv = FakeDriver(
        {"": [f("1", "01.mp4"), f("2", "02.mkv")]},
        dirs={"/动漫/测试剧": [f("x", "01.mkv")]},
    )
    res = await run_update_task(drv, spec(ignore_extension=True))
    assert [i.share_name for i in res.files] == ["02.mkv"]


@pytest.mark.asyncio
async def test_rename_after_save():
    drv = FakeDriver({"": [f("1", "第05集.mp4")]})
    res = await run_update_task(drv, spec(pattern=r"第(\d+)集", replace=r"EP\1"))
    assert drv.renamed == [("第05集.mp4", "EP05.mp4")]
    assert res.files[0].final_name == "EP05.mp4"


@pytest.mark.asyncio
async def test_startfid_traverses_new_to_old_inclusive():
    # 列表按 mtime 新→旧；startfid 命中（含）后停止
    drv = FakeDriver({"": [f("3", "03.mp4"), f("2", "02.mp4"), f("1", "01.mp4")]})
    res = await run_update_task(drv, spec(startfid="2"))
    assert [i.share_name for i in res.files] == ["03.mp4", "02.mp4"]


@pytest.mark.asyncio
async def test_single_folder_share_is_transparent():
    drv = FakeDriver({"": [d("10", "第一季")], "/第一季": [f("1", "01.mp4", "t1")]})
    res = await run_update_task(drv, spec())
    assert res.files[0].dest_path == "/动漫/测试剧/01.mp4"


@pytest.mark.asyncio
async def test_update_subdir_recursion():
    # 根含多个条目避免透明下钻；4K 目录已存在 → 递归比对子目录
    drv = FakeDriver(
        {
            "": [d("10", "4K"), f("11", "readme.mp4", "t0")],
            "/4K": [f("1", "ep1.mp4", "t1"), f("2", "ep2.mp4", "t2")],
        },
        dirs={"/动漫/测试剧": [d("9", "4K"), f("8", "readme.mp4")], "/动漫/测试剧/4K": [f("7", "ep1.mp4")]},
    )
    res = await run_update_task(drv, spec(update_subdir="4K"))
    assert [i.share_name for i in res.files] == ["ep2.mp4"]
    assert res.files[0].dest_path == "/动漫/测试剧/4K/ep2.mp4"


@pytest.mark.asyncio
async def test_update_subdir_resave_deletes_then_resaves():
    drv = FakeDriver(
        {"": [d("10", "4K"), f("11", "readme.mp4", "t0")], "/4K": [f("1", "ep1.mp4", "t1")]},
        dirs={"/动漫/测试剧": [d("9", "4K"), f("8", "readme.mp4")]},
    )
    res = await run_update_task(drv, spec(update_subdir="4K", update_subdir_resave=True))
    assert drv.deleted == ["4K"]
    assert [i.share_name for i in res.files] == ["4K"]
    assert res.files[0].is_dir


@pytest.mark.asyncio
async def test_share_banned():
    drv = FakeDriver({"": [f("1", "01.mp4")]})

    async def banned_list_share(ref, path=""):
        raise ShareBanned("分享已经失效啦")

    drv.list_share = banned_list_share  # type: ignore
    res = await run_update_task(drv, spec())
    assert res.status == "banned"
    assert "失效" in res.message


@pytest.mark.asyncio
async def test_increment_i_integration():
    # 目录已有 01.mkv（异扩展名不构成 {II}.mp4 通配去重），新文件从 02 递增
    drv = FakeDriver(
        {"": [f("1", "a.mp4", "t1", mtime=1), f("2", "b.mp4", "t2", mtime=2)]},
        dirs={"/动漫/测试剧": [f("9", "01.mkv")]},
    )
    res = await run_update_task(drv, spec(pattern=r"^.*$", replace="{II}.{EXT}"))
    assert sorted(i.final_name for i in res.files) == ["02.mp4", "03.mp4"]


@pytest.mark.asyncio
async def test_increment_i_dedup_same_template():
    # 与原项目一致：目标目录已有同模板编号文件（01.mp4），{II}.mp4 通配命中 → 视为已存在不追转
    drv = FakeDriver(
        {"": [f("1", "a.mp4", "t1", mtime=1)]},
        dirs={"/动漫/测试剧": [f("9", "01.mp4")]},
    )
    res = await run_update_task(drv, spec(pattern=r"^.*$", replace="{II}.{EXT}"))
    assert res.files == []


@pytest.mark.asyncio
async def test_unsupported_driver():
    drv = FakeDriver({"": []})
    drv.supported = False
    res = await run_update_task(drv, spec())
    assert res.status == "failed"
    assert "尚未实现" in res.message
