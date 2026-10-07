"""搜索 API：引擎类型自描述 + 选源透传 + token 按引擎回写。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api import routes_search
from backend.api.deps import get_setting, set_setting
from backend.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_engine_types_describe_their_fields(client):
    types = {t["type"]: t for t in client.get("/api/search/engine-types").json()["data"]}
    assert set(types) >= {"pansou", "cloudsaver"}
    assert types["pansou"]["default_server"] == "https://so.252035.xyz"
    ps_fields = {f["key"]: f for f in types["pansou"]["fields"]}
    assert ps_fields["server"]["required"] is False  # 留空=用内置公共站
    cs_fields = {f["key"]: f for f in types["cloudsaver"]["fields"]}
    assert cs_fields["server"]["required"] is True
    assert cs_fields["password"]["secret"] is True


def test_suggestions_pass_selected_engine_through(client, monkeypatch):
    seen = {}

    async def fake_search_all(query, deep, cfg, engine_id="", **kw):
        seen.update({"q": query, "deep": deep, "engine": engine_id})
        return {"data": [], "errors": [{"engine": "备用站", "reason": "备用站挂了"}], "token_updates": {}}

    monkeypatch.setattr(routes_search, "search_all", fake_search_all)
    body = client.get("/api/search/suggestions", params={"q": "三体", "d": "1", "engine": "e2"}).json()
    assert seen == {"q": "三体", "deep": True, "engine": "e2"}
    assert body["ok"] is True and body["errors"] == [{"engine": "备用站", "reason": "备用站挂了"}]


def test_refreshed_token_is_written_back_to_its_own_engine(client, monkeypatch):
    set_setting(
        "source",
        {
            "engines": [
                {"id": "e1", "type": "cloudsaver", "name": "家里", "server": "https://a", "token": "OLD"},
                {"id": "e2", "type": "cloudsaver", "name": "公司", "server": "https://b", "token": "KEEP"},
            ]
        },
    )

    async def fake_search_all(query, deep, cfg, engine_id="", **kw):
        return {"data": [], "errors": [], "token_updates": {"e1": "NEW"}}

    monkeypatch.setattr(routes_search, "search_all", fake_search_all)
    client.get("/api/search/suggestions", params={"q": "三体"})

    engines = get_setting("source")["engines"]
    assert {e["id"]: e["token"] for e in engines} == {"e1": "NEW", "e2": "KEEP"}


def test_settings_read_folds_old_shape_for_the_ui(client):
    """三个读口径（全量/单键/PUT 回显）必须给前端同一个形状，否则设置页看到空列表。"""
    old = {"pansou": {"server": "https://ps.test"}, "cloudsaver": {}}
    set_setting("source", old)
    assert [e["type"] for e in client.get("/api/settings").json()["source"]["engines"]] == ["pansou"]
    assert [e["type"] for e in client.get("/api/settings/source").json()["value"]["engines"]] == ["pansou"]
    echoed = client.put("/api/settings/source", json={"value": old}).json()
    assert [e["type"] for e in echoed["value"]["engines"]] == ["pansou"]
    ids = [[e["id"] for e in client.get("/api/settings").json()["source"]["engines"]] for _ in range(2)]
    assert ids[0] == ids[1]  # 折算出来的引擎 id 要稳定，前端的引擎预选按 id 记


def test_saved_new_shape_is_persisted_by_the_write_path(client):
    set_setting("source", {"pansou": {"server": "https://ps.test"}, "cloudsaver": {}})
    client.put("/api/settings/source", json={"value": {"pansou": {"server": "https://ps.test"}, "cloudsaver": {}}})
    assert "engines" in get_setting("source")  # 落库即新形状，不用等用户再存一次


def test_old_shape_config_gets_upgraded_when_token_is_written_back(client, monkeypatch):
    """老形状下搜出 token：写回时顺势落成引擎列表，别的字段不能丢。"""
    set_setting("source", {"pansou": {"server": "https://ps.test"}, "cloudsaver": {"server": "https://cs", "username": "u", "password": "p"}})

    async def fake_search_all(query, deep, cfg, engine_id="", **kw):
        cs = next(e for e in cfg["engines"] if e["type"] == "cloudsaver")
        return {"data": [], "errors": [], "token_updates": {cs["id"]: "NEW"}}

    monkeypatch.setattr(routes_search, "search_all", fake_search_all)
    client.get("/api/search/suggestions", params={"q": "三体"})

    engines = get_setting("source")["engines"]
    assert len(engines) == 2
    by_type = {e["type"]: e for e in engines}
    assert by_type["cloudsaver"]["token"] == "NEW"
    assert by_type["cloudsaver"]["username"] == "u"
    assert by_type["pansou"]["server"] == "https://ps.test"
