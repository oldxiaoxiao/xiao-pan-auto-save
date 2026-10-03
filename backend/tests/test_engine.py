"""引擎测试：用内存 FakeDriver 验证差集/忽略扩展名/startfid/子目录两种模式/重命名。"""

from __future__ import annotations

import itertools

import pytest

from backend.core.engine import TaskSpec, matches_filters, run_update_task
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


def test_matches_filters_quality_or():
    assert matches_filters("剧.S02.1080p.mkv", 0, 0, "1080p,4k") is True
    assert matches_filters("剧.4K.mkv", 0, 0, "1080p,4k") is True
    assert matches_filters("剧.720p.mkv", 0, 0, "1080p,4k") is False
    assert matches_filters("剧.mkv", 0, 0, "") is True  # 不限画质
    assert matches_filters("14k.mkv", 0, 0, "4k") is False  # 词边界，不误命中


def test_matches_filters_episode_range():
    assert matches_filters("第05集.mp4", 1, 20, "") is True
    assert matches_filters("第25集.mp4", 1, 20, "") is False
    assert matches_filters("第01集.mp4", 1, 1, "") is True  # 边界含
    assert matches_filters("第01集.mp4", 0, 0, "") is True  # 未设区间放行
    assert matches_filters("花絮无集数.mp4", 1, 20, "") is False  # 设了区间但提不出集数
    # 单边限定：另一端 0 = 不限
    assert matches_filters("第03集.mp4", 0, 5, "") is True
    assert matches_filters("第08集.mp4", 0, 5, "") is False
    assert matches_filters("第03集.mp4", 2, 0, "") is True
    assert matches_filters("第01集.mp4", 2, 0, "") is False


def test_matches_filters_empty_passes_all():
    assert matches_filters("任意文件名", 0, 0, "") is True


def test_matches_filters_quality_alias_4k_2160p():
    # 4K 与 2160p 互为别名（真实文件名常用 2160p 而非 4K）
    assert matches_filters("S01E194.2025.2160p.x264.mkv", 0, 0, "4K") is True
    assert matches_filters("S01E194.2160p.mkv", 0, 0, "2160P") is True
    assert matches_filters("S01E194.4k.mkv", 0, 0, "2160P") is True
    assert matches_filters("x.1080p.mkv", 0, 0, "1080") is True  # 1080 命中 1080p
    assert matches_filters("S01E194.1080p.mkv", 0, 0, "4K") is False  # 1080p 不算 4K
    assert matches_filters("x.2160p.mkv", 0, 0, "1080p") is False
    assert matches_filters("任意.mp4", 0, 0, ", ,") is True  # 逗号/空白 token 视为不限画质


@pytest.mark.asyncio
async def test_engine_applies_quality_filter_on_files():
    drv = FakeDriver(
        {"": [f("1", "ep1.1080p.mp4"), f("2", "ep2.4k.mp4"), f("3", "ep3.720p.mp4")]},
    )
    res = await run_update_task(drv, spec(quality="1080p,4k"))
    assert sorted(i.share_name for i in res.files) == ["ep1.1080p.mp4", "ep2.4k.mp4"]


@pytest.mark.asyncio
async def test_engine_episode_filter_skips_dirs():
    # 目录名无集数，设了区间也不能误杀整棵子树。
    # 目标已存在 4K 目录 → update_subdir 走递归比对（同 test_update_subdir_recursion）；
    # 递归进入 /4K 后按集数区间过滤叶子文件：03 命中、30 越界、readme 无集数被过滤。
    drv = FakeDriver(
        {
            "": [d("10", "4K"), f("11", "readme.mp4")],
            "/4K": [f("1", "第03集.mp4"), f("2", "第30集.mp4")],
        },
        dirs={"/动漫/测试剧": [d("9", "4K")], "/动漫/测试剧/4K": []},
    )
    res = await run_update_task(drv, spec(update_subdir="4K", episode_start=1, episode_end=10))
    assert [i.share_name for i in res.files] == ["第03集.mp4"]
    assert res.files[0].dest_path == "/动漫/测试剧/4K/第03集.mp4"


@pytest.mark.asyncio
async def test_engine_episode_filter_digitless_dir_still_recursed():
    # 「第一季」无 ASCII 数字：若去掉目录放行（not share_file.is_dir）守卫，
    # 区间过滤会误杀该目录导致不再递归——本测试用于区分这一回归。
    drv = FakeDriver(
        {
            "": [d("10", "第一季"), f("11", "readme.mp4")],
            "/第一季": [f("1", "第03集.mp4"), f("2", "第30集.mp4")],
        },
        dirs={"/动漫/测试剧": [d("9", "第一季"), f("8", "readme.mp4")], "/动漫/测试剧/第一季": []},
    )
    res = await run_update_task(drv, spec(update_subdir="第一季", episode_start=1, episode_end=10))
    assert [i.share_name for i in res.files] == ["第03集.mp4"]
    assert res.files[0].dest_path == "/动漫/测试剧/第一季/第03集.mp4"


@pytest.mark.asyncio
async def test_engine_startfid_stop_wins_over_filter():
    # FIX 回归：列表新→旧，startfid 文件本身被画质过滤拒绝时，
    # 仍须在该文件处停止——绝不能 continue 跳过 break 而越过起始点转存更旧的文件。
    drv = FakeDriver(
        {"": [f("3", "03.4k.mp4"), f("2", "02.1080p.mp4"), f("1", "01.4k.mp4")]},
    )
    res = await run_update_task(drv, spec(startfid="2", quality="4k"))
    # 03（更新且通过过滤）转存；02 命中 startfid 但被过滤 → 依然截断；01 更旧 → 不得转存
    assert [i.share_name for i in res.files] == ["03.4k.mp4"]
