"""搜索引擎配置：协议注册表、配置归一化与参与搜索的引擎选取。

同一协议可配多个实例（互为备份），配置形状为 {"engines": [引擎项, ...]}。
读入口一律先过 normalize_source_cfg：老形状 {"pansou": {...}, "cloudsaver": {...}}
在此折算成新形状，DB 不做迁移；未识别的 type 原样保留字段，交给选取环节跳过。
"""

from __future__ import annotations

from uuid import uuid4

# 协议注册表：加新协议 = 加一项 + 在 search_service.ADAPTERS 配一个同名适配器。
# fields 是给前端渲染表单用的自描述（key/label/required/secret），不在这里写死输入框。
SEARCH_ENGINES: dict[str, dict] = {
    "pansou": {
        "label": "PanSou",
        "default_server": "https://so.252035.xyz",
        "fields": [
            {"key": "server", "label": "服务器地址", "required": False, "secret": False},
        ],
    },
    "kkso": {
        "label": "夸克搜 kkso.net",
        "default_server": "https://kkso.net",
        "fields": [
            {"key": "server", "label": "服务器地址", "required": False, "secret": False},
        ],
    },
    "cloudsaver": {
        "label": "CloudSaver",
        "default_server": "",
        "fields": [
            {"key": "server", "label": "服务器地址", "required": True, "secret": False},
            {"key": "username", "label": "用户名", "required": True, "secret": False},
            {"key": "password", "label": "密码", "required": True, "secret": True},
            {"key": "token", "label": "Token", "required": False, "secret": True},
        ],
    },
}

_OLD_KEYS = ("pansou", "cloudsaver")


def engine_type_specs() -> list[dict]:
    """给前端的协议清单：每种协议怎么填、哪些必填、哪个是密钥。"""
    return [
        {
            "type": type_key,
            "label": spec["label"],
            "default_server": spec["default_server"],
            "fields": [dict(field) for field in spec["fields"]],
        }
        for type_key, spec in SEARCH_ENGINES.items()
    ]


def _new_id() -> str:
    return uuid4().hex[:8]


def _enabled(value: object) -> bool:
    """enable 兼容老配置：值可能是 "true"/"false" 字符串，缺省按启用。"""
    return str(value).lower() != "false"


def _label(type_key: str) -> str:
    return (SEARCH_ENGINES.get(type_key) or {}).get("label", type_key)


def _default_server(type_key: str) -> str:
    return str((SEARCH_ENGINES.get(type_key) or {}).get("default_server", ""))


def _fill(engine: dict) -> dict:
    type_key = str(engine.get("type", ""))
    out = dict(engine)
    out["type"] = type_key
    out["id"] = str(engine.get("id") or "") or _new_id()
    out["name"] = str(engine.get("name") or "").strip() or _label(type_key)
    out["server"] = str(engine.get("server") or "").strip() or _default_server(type_key)
    out["enable"] = _enabled(engine.get("enable", True))
    return out


def _from_old(type_key: str, cfg: dict) -> dict | None:
    server = str(cfg.get("server") or "").strip() or _default_server(type_key)
    if not server:  # 老形状里 CloudSaver 没填地址 = 没用过，不折算成一条废引擎
        return None
    # id 按协议定死而不是随机：老配置每次读都折算，随机 id 会让前端「上次用的引擎」预选失效
    return _fill({"id": f"{type_key}-legacy", "type": type_key, "enable": cfg.get("enable", True), **cfg})


def normalize_source_cfg(raw: dict | None) -> list[dict]:
    if not isinstance(raw, dict):
        return []
    engines = raw.get("engines")
    if isinstance(engines, list):
        return [_fill(e) for e in engines if isinstance(e, dict)]
    out = []
    for key in _OLD_KEYS:
        if key in raw:
            folded = _from_old(key, raw.get(key) or {})
            if folded:
                out.append(folded)
    return out


def resolve_engines(raw: dict | None, engine_id: str = "") -> tuple[list[dict], list[dict]]:
    """挑出本次参与搜索的引擎，返回 (引擎项, 不可用原因)。

    指定了 engine_id 却不可用时报错而不是退回搜全部：UI 上「搜过了」和「没搜」必须分得清。
    """
    engines = normalize_source_cfg(raw)

    def unsupported(engine: dict) -> str:
        return f"协议 {engine['type']} 暂不支持"

    if engine_id:
        hit = next((e for e in engines if e["id"] == engine_id), None)
        if hit is None:
            return [], [{"engine": engine_id, "reason": "引擎不存在或已删除，请在设置里重新选择"}]
        if not hit["enable"]:
            return [], [{"engine": hit["name"], "reason": f"{hit['name']} 已停用，请重新选择或到设置里启用"}]
        if hit["type"] not in SEARCH_ENGINES:
            return [], [{"engine": hit["name"], "reason": f"{hit['name']} {unsupported(hit)}"}]
        return [hit], []

    targets, errors = [], []
    for engine in engines:
        if not engine["enable"]:
            continue
        if engine["type"] not in SEARCH_ENGINES:
            errors.append({"engine": engine["name"], "reason": f"{engine['name']} {unsupported(engine)}，已跳过"})
            continue
        targets.append(engine)
    return targets, errors
