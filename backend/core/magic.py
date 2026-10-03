"""魔法重命名：正则/替换式 + 魔法关键字（$TV 等）+ 魔法变量（{E}/{I}/{DATE} 等）。

行为对齐已验证的交互流程：
- `pattern` 若命中 magic_regex 关键字则展开为预设正则（replace 为空时连预设替换式一起用）；
- `sub()` 先做魔法变量预处理再 re.sub；`{I}` 保留占位，由 sort_file_list 依
  目标目录现状统一编号（{I} 个数 = 补零宽度，如 {II} → 01）；
- `is_exists()` 支持忽略扩展名与 {I} 通配比对。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

from natsort import natsorted

BUILTIN_MAGIC_REGEX: dict[str, dict[str, str]] = {
    "$TV": {
        "pattern": r".*?([Ss]\d{1,2})?(?:[第EePpXx\.\-\_\( ]{1,2}|^)(\d{1,3})(?!\d).*?\.(mp4|mkv)",
        "replace": r"\1E\2.\3",
    },
    "$BLACK_WORD": {
        "pattern": r"^(?!.*纯享)(?!.*加更)(?!.*超前企划)(?!.*训练室)(?!.*蒸蒸日上).*",
        "replace": "",
    },
}

# 每个魔法变量对应候选正则列表（按序尝试）；非列表值为直接注入值
DEFAULT_MAGIC_VARIABLES: dict[str, Any] = {
    "{TASKNAME}": "",
    "{I}": 1,
    "{EXT}": [r"(?<=\.)\w+$"],
    "{CHINESE}": [r"[\u4e00-\u9fa5]{2,}"],
    "{DATE}": [
        r"(18|19|20)?\d{2}[\.\-/年]\d{1,2}[\.\-/月]\d{1,2}",
        r"(?<!\d)[12]\d{3}[01]?\d[0123]?\d",
        r"(?<!\d)[01]?\d[\.\-/月][0123]?\d",
    ],
    "{YEAR}": [r"(?<!\d)(18|19|20)\d{2}(?!\d)"],
    "{S}": [r"(?<=[Ss])\d{1,2}(?=[EeXx])", r"(?<=[Ss])\d{1,2}"],
    "{SXX}": [r"[Ss]\d{1,2}(?=[EeXx])", r"[Ss]\d{1,2}"],
    "{E}": [
        r"(?<=[Ss]\d\d[Ee])\d{1,3}",
        r"(?<=[Ee])\d{1,3}",
        r"(?<=[Ee][Pp])\d{1,3}",
        r"(?<=第)\d{1,3}(?=[集期话部篇])",
        r"(?<!\d)\d{1,3}(?=[集期话部篇])",
        r"(?!.*19)(?!.*20)(?<=[\._])\d{1,3}(?=[\._])",
        r"^\d{1,3}(?=\.\w+)",
        r"(?<!\d)\d{1,3}(?!\d)(?!$)",
    ],
    "{PART}": [
        r"(?<=[集期话部篇第])[上中下一二三四五六七八九十]",
        r"[上中下一二三四五六七八九十]",
    ],
    "{VER}": [r"[\u4e00-\u9fa5]+版"],
}

PRIORITY_LIST = list("上中下一二三四五六七八九十百千万")

_I_PATTERN = re.compile(r"\{I+\}")


class MagicRename:
    def __init__(self, magic_regex: dict[str, dict[str, str]] | None = None):
        self.magic_regex: dict[str, dict[str, str]] = dict(BUILTIN_MAGIC_REGEX)
        if magic_regex:
            self.magic_regex.update(magic_regex)
        self.magic_variable: dict[str, Any] = {k: v for k, v in DEFAULT_MAGIC_VARIABLES.items()}
        self.dir_filename_dict: dict[int, str] = {}

    def set_taskname(self, taskname: str) -> None:
        self.magic_variable["{TASKNAME}"] = taskname

    def magic_regex_conv(self, pattern: str, replace: str) -> tuple[str, str]:
        """pattern 命中魔法关键字时展开为预设正则/替换式。"""
        preset = self.magic_regex.get(pattern or "")
        if preset:
            pattern = preset["pattern"]
            if not replace:
                replace = preset.get("replace", "")
        return pattern, replace

    def sub(self, pattern: str, replace: str, file_name: str) -> str:
        """魔法变量预处理 + re.sub 替换；未匹配的变量清除，{I} 保留占位。"""
        if not replace:
            return file_name
        for key, p_list in list(self.magic_variable.items()):
            if key not in replace:
                continue
            matched: str | None = None
            if isinstance(p_list, list):
                for p in p_list:
                    if m := re.search(p, file_name):
                        value = m.group()
                        if key == "{DATE}":
                            digits = "".join(c for c in value if c.isdigit())
                            value = str(datetime.now().year)[: 8 - len(digits)] + digits
                        matched = value
                        break
            if matched is not None:
                replace = replace.replace(key, matched)
            elif key == "{TASKNAME}":
                replace = replace.replace(key, str(self.magic_variable.get("{TASKNAME}") or ""))
            elif key == "{SXX}":
                replace = replace.replace(key, "S01")
            elif key == "{I}":
                continue
            else:
                replace = replace.replace(key, "")
        if pattern and replace:
            return re.sub(pattern, replace, file_name)
        return replace

    def _custom_sort_key(self, name: str) -> str:
        for i, keyword in enumerate(PRIORITY_LIST):
            if keyword in name:
                name = name.replace(keyword, f"_{i:02d}_")
        return name

    def set_dir_file_list(self, file_list: list[dict], replace: str) -> None:
        """由替换式反推匹配式，扫描目标目录现有文件推出已用序号，避免 {I} 重复。

        file_list 每项需含 name / is_dir 键（drive-neutral FsItem.to_dict 视图）。
        """
        self.dir_filename_dict = {}
        filename_list = sorted(f["name"] for f in file_list if not f["is_dir"])
        if not filename_list:
            return
        match = _I_PATTERN.search(replace)
        if not match:
            return
        magic_i = match.group()
        pattern_i = r"\d" * magic_i.count("I")
        skeleton = replace.replace(magic_i, "🔢")
        for key in self.magic_variable:
            skeleton = skeleton.replace(key, "🔣")
        skeleton = re.sub(r"\\[0-9]+", "🔣", skeleton)  # \1 \2 等反向引用视为通配
        pattern = f"({re.escape(skeleton).replace('🔣', '.*?').replace('🔢', f')({pattern_i})(')})"
        if m := re.match(pattern, filename_list[-1]):
            self.magic_variable["{I}"] = int(m.group(2))
        for filename in filename_list:
            if m := re.match(pattern, filename):
                self.dir_filename_dict[int(m.group(2))] = m.group(1) + magic_i + m.group(3)

    def sort_file_list(self, file_list: list[dict], dir_filename_dict: dict[int, str] | None = None) -> None:
        """对待存列表统一排序并回填 {I} 序号（就地改 file['name_re']）。

        file_list 每项含 name / name_re / mtime / is_dir；目录已占序号会跳过。
        """
        dir_map = dict(dir_filename_dict or self.dir_filename_dict)
        keyed = [f"{f['name_re']}_{f['mtime']}" for f in file_list if f.get("name_re") and not f["is_dir"]]
        all_names = list(set(keyed) | set(dir_map.values()))
        all_names = natsorted(all_names, key=self._custom_sort_key)
        filename_index: dict[str, int] = {}
        for name in all_names:
            if name in dir_map.values():
                continue
            i = all_names.index(name) + 1
            while i in dir_map:
                i += 1
            dir_map[i] = name
            filename_index[name] = i
        for f in file_list:
            if not f.get("name_re"):
                continue
            if m := _I_PATTERN.search(f["name_re"]):
                i = filename_index.get(f"{f['name_re']}_{f['mtime']}", 0)
                f["name_re"] = _I_PATTERN.sub(str(i).zfill(m.group().count("I")), f["name_re"], count=1)

    def is_exists(self, filename: str, filename_list: list[str], ignore_ext: bool = False) -> str | None:
        """判断文件是否已存在；{I} 占位按数字通配比对。命中返回已存在的名字。"""
        if ignore_ext:
            filename = os.path.splitext(filename)[0]
            filename_list = [os.path.splitext(f)[0] for f in filename_list]
        if m := _I_PATTERN.search(filename):
            magic_i = m.group()
            pattern = re.escape(filename).replace(re.escape(magic_i), r"\d" * magic_i.count("I"))
            for candidate in filename_list:
                if re.match(pattern, candidate):
                    return candidate
            return None
        return filename if filename in filename_list else None
