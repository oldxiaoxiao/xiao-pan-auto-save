import pytest

from backend.core.magic import MagicRename


@pytest.fixture
def mr():
    m = MagicRename()
    m.set_taskname("凡人修仙传")
    return m


class TestMagicRegexConv:
    def test_tv_keyword(self, mr):
        pattern, replace = mr.magic_regex_conv("$TV", "")
        assert "\\1E\\2" in replace
        # 用户已给 replace 时不覆盖
        _, replace2 = mr.magic_regex_conv("$TV", "{TASKNAME}")
        assert replace2 == "{TASKNAME}"

    def test_user_preset_overrides(self):
        m = MagicRename({"$TV": {"pattern": "x", "replace": "y"}})
        pattern, replace = m.magic_regex_conv("$TV", "")
        assert (pattern, replace) == ("x", "y")

    def test_normal_pattern_untouched(self, mr):
        assert mr.magic_regex_conv(r"\d+", "\\0") == (r"\d+", "\\0")


class TestSub:
    def test_basic_rename(self, mr):
        # re.sub 与原项目一致为全局替换
        assert mr.sub(r"Episode (\d+)", r"Ep\1", "Episode 5.mp4") == "Ep5.mp4"

    def test_e_variable(self, mr):
        out = mr.sub(r"^.*$", "{TASKNAME}.{SXX}E{E}.{EXT}", "怪奇物语 S02E07.mp4")
        assert out == "凡人修仙传.S02E07.mp4"

    def test_sxx_defaults_s01(self, mr):
        out = mr.sub(r"^.*$", "{SXX}E{E}", "某剧 第12集.mp4")
        assert out.startswith("S01E12")

    def test_taskname_and_chinese(self, mr):
        out = mr.sub(r"^.*$", "{TASKNAME}-{CHINESE}", "abc 庆余年 第二季.mp4")
        assert out == "凡人修仙传-庆余年"

    def test_date_pad_year(self, mr):
        out = mr.sub(r"^.*$", "{DATE}", "2024.01.05 节目.mp4")
        assert out == "20240105"

    def test_unmatched_variable_cleared(self, mr):
        out = mr.sub(r"^.*$", "A{YEAR}B", "无年份文件.mp4")
        assert out == "AB"

    def test_i_placeholder_kept(self, mr):
        out = mr.sub(r"^.*$", "{II}.{EXT}", "01.mp4")
        assert out == "{II}.mp4"

    def test_replace_only(self, mr):
        assert mr.sub("", "固定名.mp4", "乱七八糟.mp4") == "固定名.mp4"


class TestExists:
    def test_plain(self, mr):
        assert mr.is_exists("a.mp4", ["a.mp4", "b.mkv"]) == "a.mp4"
        assert mr.is_exists("c.mp4", ["a.mp4"]) is None

    def test_ignore_extension(self, mr):
        assert mr.is_exists("01.mp4", ["01.mkv"], ignore_ext=True) == "01"

    def test_i_wildcard(self, mr):
        # name_re 里 {I} 保留占位、其余变量已展开，按数字通配比对
        assert mr.is_exists("{II}E12", ["07E12"]) == "07E12"
        assert mr.is_exists("{II}E13", ["08E12"]) is None


class TestIncrement:
    def test_continue_from_dir(self, mr):
        # 目标目录已有 01~03，新文件从 04 开始
        dir_files = [{"name": f"{i:02d}.mp4", "is_dir": False} for i in range(1, 4)]
        mr.set_dir_file_list(dir_files, "{II}.mp4")
        plans = [{"name_re": "{II}.mp4", "mtime": 100, "is_dir": False}]
        mr.sort_file_list(plans)
        assert plans[0]["name_re"] == "04.mp4"

    def test_first_number(self, mr):
        mr.set_dir_file_list([], "{II}.mp4")
        plans = [
            {"name_re": "{II}.mp4", "mtime": 1, "is_dir": False},
            {"name_re": "{II}.mp4", "mtime": 2, "is_dir": False},
        ]
        mr.sort_file_list(plans)
        assert [p["name_re"] for p in plans] == ["01.mp4", "02.mp4"]

    def test_priority_order_chinese(self, mr):
        # 上/中/下 按部序排号而非字典序
        plans = [
            {"name_re": "第{I}集.mp4", "mtime": 0, "is_dir": False},
        ]
        mr.sort_file_list(plans)
        assert plans[0]["name_re"] == "第1集.mp4"


from backend.core.magic import extract_episode


def test_extract_episode_variants():
    assert extract_episode("第05集.mp4") == 5
    assert extract_episode("凡人修仙传.E193.mkv") == 193
    assert extract_episode("S02E15.mp4") == 15
    assert extract_episode("01.mp4") == 1
    assert extract_episode("预告片 1080p.mp4") in (1080, None)  # 宽松候选可能取到 1080，允许 None
    assert extract_episode("无数字标题.mkv") is None
