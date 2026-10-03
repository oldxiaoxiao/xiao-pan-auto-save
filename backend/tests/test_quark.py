"""夸克驱动单测：全程 mock HTTP，不打真实 API。"""

from __future__ import annotations

import pytest

from backend.drivers.base import ShareBanned, ShareUnavailable
from backend.drivers.quark import BASE_URL, QuarkDriver, _match_mparam


class Recorder:
    def __init__(self, responses: list[dict]):
        self.calls: list[dict] = []
        self.responses = responses

    async def request(self, method, url, *, params=None, json=None, headers=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


@pytest.fixture
def drv():
    return QuarkDriver(cookie="UCID=abc; __uid=def")


def test_parse_share_basic(drv):
    ref = drv.parse_share("https://pan.quark.cn/s/abcd1234?pwd=xy88")
    assert ref.pwd_id == "abcd1234"
    assert ref.passcode == "xy88"
    assert ref.pdir_fid == "0"


def test_parse_share_deeplink(drv):
    fid = "0123456789abcdef0123456789abcdef"
    ref = drv.parse_share(f"https://pan.quark.cn/s/abcd#/list/share/{fid}-Season*101%202")
    assert ref.pdir_fid == fid
    assert ref.sub_names == ["Season- 2"]
    assert ref.extra["path_fids"][""] == fid


def test_mparam_from_cookie():
    assert _match_mparam("k=1;#kps=AAA%25BB&sign=CCC&vcode=DDD") == {
        "kps": "AAA%BB",
        "sign": "CCC",
        "vcode": "DDD",
    }
    assert _match_mparam("k=1") is None


@pytest.mark.asyncio
async def test_mobile_routing(drv):
    drv.mparam = {"kps": "K", "sign": "S", "vcode": "V"}
    drv.client = Recorder([{"status": 200, "code": 0, "data": {"stoken": "stk"}}])
    ref = drv.parse_share("https://pan.quark.cn/s/abcd")
    await drv._get_stoken(ref)
    call = drv.client.calls[0]
    assert "drive-m.quark.cn" in call["url"]
    assert call["params"]["kps"] == "K"
    assert call["headers"]["cookie"] is None  # 匿名化：去掉 Cookie
    assert call["json"] == {"pwd_id": "abcd", "passcode": ""}


@pytest.mark.asyncio
async def test_pc_routing_keeps_cookie(drv):
    drv.client = Recorder([{"status": 200, "code": 0, "data": {"list": [], "full_path": []}}])
    ref = drv.parse_share("https://pan.quark.cn/s/abcd")
    ref.extra["stoken"] = "stk"
    await drv.list_share(ref, "")
    call = drv.client.calls[0]
    call = drv.client.calls[0]
    assert call["url"].startswith(BASE_URL)
    assert call["headers"] is None  # 使用客户端默认 Cookie 头


@pytest.mark.asyncio
async def test_stoken_three_states(drv):
    drv.client = Recorder([{"status": 500, "code": 1, "message": "request error"}])
    ref = drv.parse_share("https://pan.quark.cn/s/abcd")
    with pytest.raises(ShareUnavailable):
        await drv._get_stoken(ref)

    drv.client = Recorder([{"status": 410, "code": -1, "message": "分享已被取消"}])
    with pytest.raises(ShareBanned):
        await drv._get_stoken(ref)


@pytest.mark.asyncio
async def test_list_share_pagination_and_tokens(drv):
    page1 = {
        "status": 200,
        "code": 0,
        "data": {
            "list": [
                {
                    "fid": "a" * 32,
                    "file_name": "剧集",
                    "dir": True,
                    "updated_at": 5,
                    "share_fid_token": "tokD",
                    "pdir_fid": "0",
                },
                {
                    "fid": "f1",
                    "file_name": "01.mp4",
                    "dir": False,
                    "updated_at": 9,
                    "share_fid_token": "tok1",
                    "size": 100,
                },
            ]
        },
        "metadata": {"_total": 2},
    }
    drv.client = Recorder(
        [
            {"status": 200, "code": 0, "data": {"stoken": "stk"}},
            page1,
        ]
    )
    ref = drv.parse_share("https://pan.quark.cn/s/abcd")
    items = await drv.list_share(ref, "")
    assert [i.name for i in items] == ["剧集", "01.mp4"]
    assert items[1].token == "tok1"
    assert items[0].is_dir
    # 子目录路径已进缓存
    assert ref.extra["path_fids"]["/剧集"] == "a" * 32
    detail_params = drv.client.calls[1]["params"]
    assert detail_params["_sort"] == "file_type:asc,updated_at:desc"
    assert detail_params["stoken"] == "stk"


@pytest.mark.asyncio
async def test_list_share_subpath_without_leading_slash_no_recursion(drv):
    # 回归：preview 传入裸名 "剧集"（无前导斜杠）曾令 parent==path 无限递归 → RecursionError/500
    folder = {"fid": "a" * 32, "file_name": "剧集", "dir": True, "updated_at": 5, "share_fid_token": "tD"}
    child = {"fid": "c1", "file_name": "01.mp4", "dir": False, "updated_at": 9, "share_fid_token": "t1", "size": 10}
    root_detail = {"status": 200, "code": 0, "data": {"list": [folder]}, "metadata": {"_total": 1}}
    sub_detail = {"status": 200, "code": 0, "data": {"list": [child]}, "metadata": {"_total": 1}}
    drv.client = Recorder([root_detail, sub_detail])
    ref = drv.parse_share("https://pan.quark.cn/s/abcd")
    ref.extra["stoken"] = "stk"  # 跳过换 stoken 调用
    items = await drv.list_share(ref, "剧集")
    assert [i.name for i in items] == ["01.mp4"]
    assert ref.extra["path_fids"]["/剧集"] == "a" * 32  # 归一化后与引擎共用同一缓存键


@pytest.mark.asyncio
async def test_save_body_and_batching(drv):
    n = 120
    responses = [
        {"status": 200, "code": 0, "data": {"stoken": "stk"}},
    ]
    for _ in range(2):  # 两批
        responses.append({"status": 200, "code": 0, "data": {"task_id": "t9"}})
        fids = [f"new{i}" for i in range(100)]
        responses.append(
            {"status": 200, "code": 0, "data": {"status": 2, "save_as": {"save_as_top_fids": fids}}}
        )
    drv.client = Recorder(responses)
    from backend.drivers.base import FsItem

    items = [FsItem(fid=f"s{i}", name=f"{i}.mp4", token=f"tk{i}", mtime=i) for i in range(n)]
    drv.savepath_fid["/目标"] = "destfid"
    ref = drv.parse_share("https://pan.quark.cn/s/abcd")
    result = await drv.save(items, "/目标", ref)
    assert result.ok and len(result.saved) == n
    save_calls = [c for c in drv.client.calls if c["method"] == "POST" and "sharepage/save" in c["url"]]
    assert len(save_calls) == 2
    first = save_calls[0]["json"]
    assert first["to_pdir_fid"] == "destfid"
    assert first["fid_token_list"] == [f"tk{i}" for i in range(100)]
    assert first["pwd_id"] == "abcd" and first["stoken"] == "stk"
    assert first["scene"] == "link" and first["pdir_fid"] == "0"
    assert "__dt" in drv.client.calls[2]["params"] and "__t" in drv.client.calls[2]["params"]
    # 第二批只有 20 个
    assert len(save_calls[1]["json"]["fid_list"]) == 20


@pytest.mark.asyncio
async def test_list_dir_requires_risk_filename_flag(drv):
    drv.savepath_fid["/目标"] = "destfid"
    drv.client = Recorder(
        [
            {
                "status": 200,
                "code": 0,
                "data": {"list": [{"fid": "x", "file_name": "01.mp4", "dir": False, "size": 1}]},
                "metadata": {"_total": 1},
            },
        ]
    )
    items = await drv.list_dir("/目标")
    assert items[0].name == "01.mp4"
    assert drv.client.calls[0]["params"]["fetch_risk_file_name"] == 1


@pytest.mark.asyncio
async def test_ensure_dir_creates_when_missing(drv):
    drv.client = Recorder(
        [
            {"status": 200, "code": 0, "data": []},  # path_list 未命中
            {"status": 200, "code": 0, "data": {"fid": "newdir"}},
        ]
    )
    fid = await drv.ensure_dir("/新建/目录")
    assert fid == "newdir"
    create_body = drv.client.calls[1]["json"]
    assert create_body["dir_path"] == "/新建/目录" and create_body["dir_init_lock"] is False


@pytest.mark.asyncio
async def test_sign_without_mparam_skips(drv):
    result = await drv.sign()
    assert not result.ok and "kps" in result.message
