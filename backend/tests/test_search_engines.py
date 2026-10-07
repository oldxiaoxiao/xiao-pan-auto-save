"""引擎配置归一化与选取测试：纯函数，无网络。"""

from __future__ import annotations

from backend.services.search_engines import SEARCH_ENGINES, normalize_source_cfg, resolve_engines


def test_old_shape_folds_into_engine_list():
    engines = normalize_source_cfg(
        {
            "pansou": {"server": "https://ps.test", "enable": "true"},
            "cloudsaver": {"server": "https://cs.test", "username": "u", "password": "p", "token": "t"},
        }
    )
    assert [e["type"] for e in engines] == ["pansou", "cloudsaver"]
    assert engines[0]["server"] == "https://ps.test"
    assert engines[1]["username"] == "u" and engines[1]["token"] == "t"
    assert all(e["id"] for e in engines)
    assert all(e["name"] for e in engines)  # 老配置没名字 → 用协议默认名
    assert all(e["enable"] is True for e in engines)


def test_old_shape_blank_pansou_server_falls_back_to_default_site():
    """设置里服务器留空 = 用内置公共站，不是悄悄不搜（这是原来 search_all 的静默失效）。"""
    engines = normalize_source_cfg({"pansou": {"server": ""}, "cloudsaver": {"server": ""}})
    assert len(engines) == 1  # 没填地址的 CloudSaver 不折算成废引擎
    assert engines[0]["type"] == "pansou"
    assert engines[0]["server"] == "https://so.252035.xyz"


def test_enable_string_false_normalizes_to_false():
    engines = normalize_source_cfg({"pansou": {"server": "https://ps.test", "enable": "false"}})
    assert engines[0]["enable"] is False


def test_new_shape_keeps_fields_and_engines_of_unregistered_type():
    """以后新增的协议字段/类型存进来要原样留着，归一化不能把配置改坏。"""
    engines = normalize_source_cfg(
        {"engines": [{"id": "x1", "type": "futuresite", "name": "新协议", "server": "https://n.test", "api_key": "k"}]}
    )
    assert engines[0]["id"] == "x1"
    assert engines[0]["api_key"] == "k"
    assert engines[0]["enable"] is True


def test_new_shape_empty_engines_stays_empty():
    """用户把引擎删干净了就是删干净了，不凭空塞一条默认引擎回去。"""
    assert normalize_source_cfg({"engines": []}) == []


CFG_TWO = {
    "engines": [
        {"id": "e1", "type": "pansou", "name": "公共站", "server": "https://ps1.test"},
        {"id": "e2", "type": "cloudsaver", "name": "家里", "server": "https://cs.test", "username": "u", "password": "p"},
    ]
}


def test_resolve_all_takes_every_enabled_engine():
    targets, errors = resolve_engines(CFG_TWO)
    assert [e["id"] for e in targets] == ["e1", "e2"]
    assert errors == []


def test_resolve_all_skips_disabled_engine():
    cfg = {"engines": [*CFG_TWO["engines"][:1], {**CFG_TWO["engines"][1], "enable": False}]}
    targets, errors = resolve_engines(cfg)
    assert [e["id"] for e in targets] == ["e1"]
    assert errors == []


def test_resolve_single_engine_by_id():
    targets, errors = resolve_engines(CFG_TWO, "e2")
    assert [e["id"] for e in targets] == ["e2"]
    assert errors == []


def test_resolve_missing_engine_id_reports_instead_of_falling_back():
    """下拉里选的引擎被删了：明确报错，不能悄悄退回搜全部（用户会以为搜过了）。"""
    targets, errors = resolve_engines(CFG_TWO, "gone")
    assert targets == []
    assert errors[0]["reason"] == "引擎不存在或已删除，请在设置里重新选择"


def test_resolve_disabled_engine_id_reports():
    cfg = {"engines": [{**CFG_TWO["engines"][0], "enable": False}]}
    targets, errors = resolve_engines(cfg, "e1")
    assert targets == []
    assert errors[0]["engine"] == "公共站"
    assert "已停用" in errors[0]["reason"]


def test_default_settings_seed_engine_with_stable_id():
    """默认那条 PanSou 公共实例每次读到的 id 要一致：前端的「上次用的引擎」预选按 id 记。"""
    from backend.api.deps import DEFAULT_SETTINGS

    ids = [[e["id"] for e in normalize_source_cfg(DEFAULT_SETTINGS["source"])] for _ in range(2)]
    assert ids[0] == ids[1] == ["pansou-default"]
    assert normalize_source_cfg(DEFAULT_SETTINGS["source"])[0]["server"] == "https://so.252035.xyz"


def test_protocols_registered_for_config_all_have_adapters():
    """注册表加了协议却忘写适配器，会让它在 UI 里可选却搜不动——两边必须一致。"""
    from backend.services.search_service import ADAPTERS

    assert set(ADAPTERS) == set(SEARCH_ENGINES)


def test_resolve_unregistered_selected_engine_reports():
    cfg = {"engines": [{"id": "e9", "type": "futuresite", "name": "未来站", "server": "https://n"}]}
    targets, errors = resolve_engines(cfg, "e9")
    assert targets == []
    assert "futuresite" in errors[0]["reason"]


def test_resolve_unregistered_type_is_skipped_with_reason():
    """不认识的新协议类型不参与搜索，但要告诉用户为什么少了一个源。"""
    cfg = {"engines": [CFG_TWO["engines"][0], {"id": "e9", "type": "futuresite", "name": "未来站", "server": "https://n"}]}
    targets, errors = resolve_engines(cfg)
    assert [e["id"] for e in targets] == ["e1"]
    assert errors[0]["engine"] == "未来站"
    assert "futuresite" in errors[0]["reason"]
