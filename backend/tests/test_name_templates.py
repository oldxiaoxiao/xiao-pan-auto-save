"""FR-10 命名模板库：预设能真用、预览与实际转存同源、未命中样本要露出来。"""

from __future__ import annotations

import pytest

from backend.core.engine import UNMATCHED_SAMPLE_LIMIT, TaskRunResult
from backend.services import name_template_service as svc

# ------------------------------------------------------------ 内置模板


def test_builtin_templates_are_complete():
    for t in svc.builtin():
        assert t["id"] and t["name"] and t["desc"]
        assert t["replace"], f"{t['name']} 必须给替换式，否则套用了等于没做"
        assert t["samples"], f"{t['name']} 必须自带示例，否则用户看不到效果"


def test_tv_template_normalizes_chinese_episode():
    """验收标准：选「国产剧 SxxExx」后，『第01集.mp4』应变成可预期的 S01E01.mp4。"""
    rows = svc.preview("", "{SXX}E{E}.{EXT}", ["第01集.mp4"], "凡人修仙传")
    assert rows[0]["after"] == "S01E01.mp4", rows
    assert rows[0]["matched"] is True


def test_tv_template_keeps_existing_season():
    """已经有 S02E05 的不能退化成 S01E05——丢季号等于把两季混在一起。"""
    rows = svc.preview("", "{SXX}E{E}.{EXT}", ["S02E05.mkv"], "x")
    assert rows[0]["after"] == "S02E05.mkv", rows


def test_black_word_template_filters_but_renames():
    """过滤花絮模板：正片改名，纯享版不命中（不转存）。"""
    rows = svc.preview("$BLACK_WORD", "{TASKNAME}.{DATE}.{EXT}", ["2024.05.01 期.mp4", "2024.05.01 纯享版.mp4"], "某综艺")
    assert rows[0]["matched"] is True and rows[0]["after"] == "某综艺.20240501.mp4"
    assert rows[1]["matched"] is False, "花絮必须被过滤掉"
    assert rows[1]["changed"] is False


def test_index_template_marks_pending():
    """{I} 序号要等转存时按目标目录现状推算，预览里必须说清"待定"而不是假装算出来了。"""
    rows = svc.preview("", "{TASKNAME}.{II}.{EXT}", ["某动画 01.mkv"], "某动画")
    assert rows[0]["pending_index"] is True
    assert "{I" in rows[0]["after"]


def test_empty_pattern_matches_everything():
    """内置模板刻意留空 pattern：留空 = 全部命中，比复杂正则漏配导致静默丢文件安全。"""
    rows = svc.preview("", "{TASKNAME}.{EXT}", ["随便什么名字.mp4"], "剧")
    assert rows[0]["matched"] is True


def test_preview_without_replace_leaves_name_alone():
    """只给正则不给替换式 = 只筛选不改名，这是既有语义，不能悄悄改掉。"""
    rows = svc.preview(r".*\.mp4", "", ["a.mp4", "b.mkv"], "x")
    assert rows[0]["matched"] is True and rows[0]["after"] == "a.mp4"
    assert rows[1]["matched"] is False


# ------------------------------------------------------------ 个人模板


def test_custom_template_roundtrip():
    from backend.api.deps import get_setting

    svc.save_custom("我的模板", r".*", "{TASKNAME}-{E}.{EXT}", "说明")
    rows = svc.custom()
    assert any(t["name"] == "我的模板" for t in rows)
    assert get_setting("name_templates")

    tid = next(t["id"] for t in rows if t["name"] == "我的模板")
    svc.delete_custom(tid)
    assert not any(t["name"] == "我的模板" for t in svc.custom())


def test_custom_templates_survive_garbage():
    from backend.api.deps import set_setting

    set_setting("name_templates", {"not": "a list"})
    assert svc.custom() == []
    set_setting("name_templates", [{"no_name": 1}, {"name": "有效"}])
    assert len(svc.custom()) == 1


# ------------------------------------------------------------ 未命中样本要露出来


def test_result_carries_unmatched_samples():
    """试跑必须把"正则没命中"的样本带出来。

    过去这类文件被静默丢弃，试跑只说"0 个新增"，用户分不清是正则写错还是真没更新。
    """
    r = TaskRunResult()
    assert r.unmatched_samples == []
    r.unmatched_samples.append("没匹配上的文件.mp4")
    assert len(r.unmatched_samples) == 1
    assert UNMATCHED_SAMPLE_LIMIT >= 5, "样本上限太小就失去了诊断价值"


@pytest.mark.asyncio
async def test_engine_records_unmatched_when_pattern_misses():
    """真跑引擎的目录比对：pattern 不匹配的文件要进 unmatched_samples，不能无声消失。"""
    from backend.core import engine
    from backend.drivers.base import FsItem, ShareRef

    class Driver:
        key = "fake"

        async def list_dir(self, path):
            return []

        async def ensure_dir(self, path):
            return FsItem(fid="d", name="d", is_dir=True)

        async def list_share(self, ref, path=""):
            return []

        def has(self, cap):
            return False

    def item(name: str, fid: str):
        return FsItem(fid=fid, name=name, is_dir=False, size=10, mtime=1)

    spec = engine.TaskSpec(
        taskname="剧", shareurl="https://fake.example/s/1", savepath="/剧",
        pattern=r"^绝不匹配\.mp4$", replace="{TASKNAME}.{EXT}",
    )
    result = engine.TaskRunResult()
    share = [item("第01集.mp4", "1"), item("第02集.mp4", "2")]

    await engine._check_dir(
        Driver(), spec, ShareRef(url=spec.shareurl, extra={"path_fids": {"": "0"}}), None,
        "", "", share, lambda level, msg: None, result, plan_only=True,
    )

    assert result.unmatched_samples == ["第01集.mp4", "第02集.mp4"], result.unmatched_samples
    assert result.files == [], "没命中的文件绝不能进入转存计划"


@pytest.mark.asyncio
async def test_engine_matched_files_are_not_listed_as_unmatched():
    """别误伤：命中正则的文件不该出现在未命中样本里。"""
    from backend.core import engine
    from backend.drivers.base import FsItem, ShareRef

    class Driver:
        key = "fake"

        async def list_dir(self, path):
            return []

        async def ensure_dir(self, path):
            return FsItem(fid="d", name="d", is_dir=True)

        async def list_share(self, ref, path=""):
            return []

        def has(self, cap):
            return False

    spec = engine.TaskSpec(
        taskname="剧", shareurl="https://fake.example/s/1", savepath="/剧",
        pattern="", replace="{SXX}E{E}.{EXT}",  # 留空 = 全部命中
    )
    result = engine.TaskRunResult()
    share = [FsItem(fid="1", name="第01集.mp4", is_dir=False, size=10, mtime=1)]

    await engine._check_dir(
        Driver(), spec, ShareRef(url=spec.shareurl, extra={"path_fids": {"": "0"}}), None,
        "", "", share, lambda level, msg: None, result, plan_only=True,
    )

    assert result.unmatched_samples == []
    assert len(result.files) == 1
    assert result.files[0].final_name == "S01E01.mp4"
