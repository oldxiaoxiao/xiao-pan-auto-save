"""FR-10 命名模板库：内置预设 + 个人模板 + 套用前预览。

要解决的不是"能不能写正则"（一直能），而是新手面对两个空框（pattern / replace）根本无从下手，
写错了也不知道——试跑只说"0 个新增"，看不出是正则没匹配上还是真没更新。

所以本模块做三件事：给可一键套用的预设、套用前就能看到效果、个人模板可复用。

刻意不做的事：
- **不新增一套自己的重命名语法**。全部基于既有 `MagicRename`（$关键字 + {变量}），
  预览与真实转存走同一个 `sub()`，不存在"预览对了实际不对"的可能。
- **模板不自动改写任务**：套用只填 pattern/replace 两个框，改不改由用户决定，且仍要试跑。
"""

from __future__ import annotations

import re
import uuid

from ..core.magic import MagicRename

# 内置模板：pattern 尽量留空（空=全部命中），靠 replace 里的魔法变量做规范化。
# 空 pattern 意味着"不会漏掉文件"，比写一条复杂正则更安全——复杂正则一旦漏配就是静默丢文件。
BUILTIN_TEMPLATES: list[dict] = [
    {
        "id": "tv_sxxexx",
        "name": "剧集 / 动漫（SxxExx）",
        "desc": "最常用。文件名里有 S01E02 会保留季号，只有集数（如「第01集」）时按 S01 处理。",
        "pattern": "",
        "replace": "{SXX}E{E}.{EXT}",
        "samples": ["第01集.mp4", "S02E05.mkv", "凡人修仙传 第174集 4K.mp4"],
    },
    {
        "id": "tv_with_name",
        "name": "剧集（带剧名）",
        "desc": "在 SxxExx 前加上剧名，适合多个剧共用一个目录。",
        "pattern": "",
        "replace": "{TASKNAME}.{SXX}E{E}.{EXT}",
        "samples": ["第01集.mp4", "S02E05.mkv"],
    },
    {
        "id": "variety_date",
        "name": "综艺（按日期）",
        "desc": "按文件名里的日期命名，需要文件名中含 2024.05.01 之类的日期。",
        "pattern": "",
        "replace": "{TASKNAME}.{DATE}.{EXT}",
        "samples": ["2024.05.01 期.mp4", "20240501 纯享版.mkv"],
    },
    {
        "id": "variety_filtered",
        "name": "综艺（过滤花絮）",
        "desc": "同上，但会过滤掉纯享/加更/超前企划/训练室等花絮，只留下正片。",
        "pattern": "$BLACK_WORD",
        "replace": "{TASKNAME}.{DATE}.{EXT}",
        "samples": ["2024.05.01 期.mp4", "2024.05.01 纯享版.mp4"],
    },
    {
        "id": "anime_index",
        "name": "动画（连续编号）",
        "desc": "按转存顺序自动编号（01、02…），序号由系统依目标目录现状推算，不会撞号。",
        "pattern": "",
        "replace": "{TASKNAME}.{II}.{EXT}",
        "samples": ["[字幕组] 某动画 01.mkv", "某动画 第2话.mp4"],
    },
    {
        "id": "movie_year",
        "name": "电影（片名.年份）",
        "desc": "取文件名里的中文片名与年份，适合交给 Jellyfin/Plex 刮削。",
        "pattern": "",
        "replace": "{CHINESE}.{YEAR}.{EXT}",
        "samples": ["某电影.2024.1080p.mp4", "某电影 (2024).mkv"],
    },
]

CUSTOM_KEY = "name_templates"  # 个人模板存在 setting 表，与 magic_regex 同一套做法


def builtin() -> list[dict]:
    return [dict(t) for t in BUILTIN_TEMPLATES]


def custom() -> list[dict]:
    from ..api.deps import get_setting

    raw = get_setting(CUSTOM_KEY) or []
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, dict) and t.get("name")]


def save_custom(name: str, pattern: str, replace: str, desc: str = "") -> list[dict]:
    from ..api.deps import set_setting

    rows = custom()
    rows.append(
        {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "pattern": pattern,
            "replace": replace,
            "desc": desc,
        }
    )
    set_setting(CUSTOM_KEY, rows)
    return rows


def delete_custom(template_id: str) -> list[dict]:
    from ..api.deps import set_setting

    rows = [t for t in custom() if t.get("id") != template_id]
    set_setting(CUSTOM_KEY, rows)
    return rows


def preview(pattern: str, replace: str, samples: list[str], taskname: str = "") -> list[dict]:
    """对每个样本算一次真实重命名结果，并标出正则有没有命中。

    复用 MagicRename.sub —— 与真实转存同一段代码，预览出来什么样，转存就是什么样。
    """
    mr = MagicRename()
    mr.set_taskname(taskname)
    pat, rep = mr.magic_regex_conv(pattern, replace)
    out: list[dict] = []
    for name in samples or []:
        after = mr.sub(pat, rep, name)
        out.append(
            {
                "before": name,
                "after": after or name,
                # 命中判据与引擎一致：re.search(pattern or "", name)
                "matched": bool(re.search(pat or "", name)),
                # {I} 要等转存时按目标目录现状编号，预览里算不出来，得说清楚
                "pending_index": "{I" in (after or ""),
                "changed": bool(after) and after != name,
            }
        )
    return out
