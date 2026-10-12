"""本机目录浏览与"在文件管理器中打开"的行为测试。

不触碰真实 GUI：跨线程弹对话框在 CI 里既无 Display 也无法断言，所以这块能力被设计成
后端列目录 + 前端渲染，就是为了让它**可测**。reveal 只对构造出的命令做断言，不真启动文件管理器。
"""

from __future__ import annotations

import os
import sys

import pytest

from backend.services import local_fs as lf


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "剧集").mkdir()
    (tmp_path / "Movies").mkdir()
    (tmp_path / "readme.txt").write_text("x", encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------ 列目录


def test_list_dir_returns_subdirectories_only(tree):
    """只挑"下到哪里"，文件不该被列出来。"""
    data = lf.list_dir(str(tree))
    assert [e["name"] for e in data["entries"]] == ["Movies", "剧集"]
    assert all(e["is_dir"] for e in data["entries"])


def test_list_dir_sorts_case_insensitively(tree):
    """大小写混排时按字母直觉排序，而不是让大写全挤在前面。"""
    names = [e["name"] for e in lf.list_dir(str(tree))["entries"]]
    assert names == sorted(names, key=str.lower)


def test_list_dir_defaults_to_home_when_path_empty():
    data = lf.list_dir("")
    assert data["path"] == str(lf.default_path())
    assert data["exists"] is True


def test_list_dir_rejects_missing_and_file(tree):
    with pytest.raises(lf.FsError, match="目录不存在"):
        lf.list_dir(str(tree / "压根没有"))
    with pytest.raises(lf.FsError, match="不是目录"):
        lf.list_dir(str(tree / "readme.txt"))


def test_list_dir_reports_parent_and_root():
    """根目录没有 parent——否则前端"上一级"会原地打转。"""
    root = os.path.abspath(os.sep)
    assert lf.list_dir(root)["parent"] is None
    child = lf.list_dir(os.path.join(root, ""))["path"]
    assert child == os.path.realpath(root)


def test_list_dir_skips_unreadable_entries_without_failing(tmp_path, monkeypatch):
    """个别子目录没权限，不该让整个列表失败——跳过即可。"""
    class _Entry:
        """一个没法判定类型的条目：模拟没权限读取的子目录。"""

        def is_dir(self, follow_symlinks=True):
            raise OSError("permission denied")

    class _Boom:
        def __init__(self, path):
            self._path = path

        def __iter__(self):
            yield _Entry()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(lf.os, "scandir", lambda p: _Boom(str(p)))
    assert lf.list_dir(str(tmp_path))["entries"] == []


# ------------------------------------------------------------------ 快捷入口


def test_roots_include_home():
    assert any(r["name"] == "主目录" for r in lf.roots())


def test_roots_include_existing_downloads(monkeypatch, tmp_path):
    """用户主目录下有"下载"时给个一键入口，省得从根目录点进去。"""
    monkeypatch.setattr(lf.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Downloads").mkdir()
    paths = [r["path"] for r in lf.roots()]
    assert str(tmp_path / "Downloads") in paths


# ------------------------------------------------------------------ 打开文件管理器


@pytest.fixture()
def capture(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(lf, "_run", calls.append)
    return calls


def test_reveal_selects_file_on_windows(capture, tree, monkeypatch):
    monkeypatch.setattr(lf, "_platform_key", lambda: "windows")
    result = lf.reveal(str(tree / "readme.txt"))
    assert result["select_supported"] is True
    assert capture[0][:2] == ["explorer", "/select,"]


def test_reveal_selects_file_on_macos(capture, tree, monkeypatch):
    monkeypatch.setattr(lf, "_platform_key", lambda: "macos")
    lf.reveal(str(tree / "readme.txt"))
    assert capture[0] == ["open", "-R", str(tree / "readme.txt")]


def test_reveal_opens_parent_dir_on_linux(capture, tree, monkeypatch):
    """xdg-open 没有"选中文件"语义，退化为打开所在目录，并如实标注不支持选中。"""
    monkeypatch.setattr(lf, "_platform_key", lambda: "linux")
    result = lf.reveal(str(tree / "readme.txt"))
    assert result["select_supported"] is False
    assert capture[0] == ["xdg-open", str(tree)]


def test_reveal_directory_opens_it_itself(capture, tree, monkeypatch):
    monkeypatch.setattr(lf, "_platform_key", lambda: "macos")
    lf.reveal(str(tree))
    assert capture[0] == ["open", str(tree)]


def test_reveal_missing_path_reports_error(tree):
    with pytest.raises(lf.FsError, match="不存在"):
        lf.reveal(str(tree / "没了"))


def test_reveal_reports_missing_opener(capture, tree, monkeypatch):
    """容器里通常没有 xdg-open/explorer：要给出人话，而不是让请求变成 500。"""

    def boom(_cmd):
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(lf, "_run", boom)
    monkeypatch.setattr(lf, "_platform_key", lambda: "linux")
    with pytest.raises(lf.FsError, match="未能打开文件管理器"):
        lf.reveal(str(tree))


def test_platform_key_reflects_current_system():
    if sys.platform == "darwin":
        assert lf._platform_key() == "macos"
    elif sys.platform == "win32":
        assert lf._platform_key() == "windows"
    else:
        assert lf._platform_key() == "linux"
