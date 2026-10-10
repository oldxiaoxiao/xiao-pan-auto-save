"""追更引擎：差集比对、魔法重命名、子目录递归/重存、起始文件订阅。

只依赖 CloudDrive 抽象接口，不含任何网盘专有逻辑。
算法骨架对齐原项目已验证的行为（差集、忽略扩展名、{I} 递增、startfid 截断等）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..drivers.base import (
    CloudDrive,
    DriveError,
    FsItem,
    ShareBanned,
    ShareRef,
    ShareUnavailable,
)
from .magic import MagicRename, extract_episode

LogFn = Callable[[str, str], None]

_I_RE = re.compile(r"\{I+\}")

# FR-10：试跑带出去的「正则未命中」样本上限。够了能看出问题，又不至于把整份分享列表塞进响应
UNMATCHED_SAMPLE_LIMIT = 20


@dataclass
class TaskSpec:
    taskname: str
    shareurl: str
    savepath: str
    pattern: str = ""
    replace: str = ""
    ignore_extension: bool = False
    startfid: str = ""
    update_subdir: str = ""
    update_subdir_resave: bool = False
    episode_start: int = 0
    episode_end: int = 0
    quality: str = ""


_QUALITY_GROUPS: list[set[str]] = [
    {"4k", "2160p", "2160"},
    {"1080p", "1080"},
    {"720p", "720"},
]


def _quality_variants(tok: str) -> set[str]:
    """把画质 token 扩展成同义组（4K↔2160p 等），未分组则原样返回。"""
    for grp in _QUALITY_GROUPS:
        if tok in grp:
            return grp
    return {tok}


def matches_filters(name: str, ep_start: int, ep_end: int, quality: str) -> bool:
    """叶子文件后置过滤：画质 token（词边界、OR、别名）+ 集数区间。

    仅对非目录文件调用（目录由调用方恒放行）。ep 区间与 quality 全默认时恒 True。
    """
    if quality:
        low = name.lower()
        toks = [t.strip().lower() for t in quality.split(",") if t.strip()]
        cands = {v for t in toks for v in _quality_variants(t)}
        if cands and not any(re.search(rf"(?<![0-9a-z]){re.escape(v)}(?![0-9a-z])", low) for v in cands):
            return False
    if ep_start or ep_end:
        ep = extract_episode(name)
        if ep is None:
            return False
        if ep_start and ep < ep_start:
            return False
        if ep_end and ep > ep_end:
            return False
    return True


@dataclass
class SavedFile:
    share_name: str
    final_name: str
    new_fid: str
    dest_path: str
    is_dir: bool = False
    size: int = 0  # 字节数：试跑 total_size 的来源；真实运行也照实填，纯增字段不改行为


@dataclass
class TaskRunResult:
    status: str = "no_changes"  # updated | no_changes | banned | network | failed
    message: str = ""
    files: list[SavedFile] = field(default_factory=list)
    planned_existing: int = 0  # 因"目标目录里已有"而跳过的条目数（目录不算，它会继续递归）
    filtered_out: int = 0  # 被集数/画质过滤拒掉的条目数
    # FR-10：命名正则没匹配上的样本。过去这类文件被静默丢掉，试跑显示"0 个新增"，
    # 新手根本分不清是"正则写错了"还是"真的没有更新"——必须把样本带出来
    unmatched_samples: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = []
        by_dir: dict[str, list[SavedFile]] = {}
        for f in self.files:
            by_dir.setdefault(f.dest_path, []).append(f)
        for dest, files in by_dir.items():
            lines.append(f"📁 {dest}")
            for f in files:
                icon = "📁" if f.is_dir else "📄"
                renamed = f"  (原: {f.share_name})" if f.final_name != f.share_name else ""
                lines.append(f"  {icon}{f.final_name}{renamed}")
        return "\n".join(lines)


@dataclass
class _Plan:
    """一个待转存条目：分享条目 + 计划重命名后的名字。"""

    item: FsItem
    name_re: str


def _norm_path(path: str) -> str:
    return re.sub(r"/{2,}", "/", f"/{path.strip('/')}") if path.strip("/") else "/"


def _mr_view(items: list[FsItem]) -> list[dict[str, Any]]:
    return [{"name": i.name, "is_dir": i.is_dir} for i in items]


async def run_update_task(
    driver: CloudDrive,
    spec: TaskSpec,
    magic_regex: dict[str, dict[str, str]] | None = None,
    log: LogFn | None = None,
    plan_only: bool = False,
) -> TaskRunResult:
    log = log or (lambda level, msg: None)
    result = TaskRunResult()

    if not driver.supported:
        result.status = "failed"
        result.message = f"{driver.name} 驱动尚未实现（即将支持）"
        return result

    try:
        ref = driver.parse_share(spec.shareurl)
    except DriveError as exc:
        result.status = "failed"
        result.message = str(exc)
        return result

    try:
        share_root = await driver.list_share(ref, "")
        if not share_root:
            result.status = "banned"
            result.message = "分享为空，文件已被分享者删除"
            return result
        # 分享仅一个文件夹：透明下钻一层
        if len(share_root) == 1 and share_root[0].is_dir:
            log("info", "🧠 该分享是一个文件夹，读取文件夹内列表")
            share_path, share_root = (
                f"/{share_root[0].name}",
                await driver.list_share(ref, f"/{share_root[0].name}"),
            )
        else:
            share_path = ""
        await _check_dir(driver, spec, ref, magic_regex, share_path, "", share_root, log, result, plan_only)
    except ShareBanned as exc:
        result.status = "banned"
        result.message = exc.message
    except ShareUnavailable as exc:
        result.status = "network"
        result.message = exc.message
    except DriveError as exc:
        result.status = "failed"
        result.message = exc.message
    return result


async def _check_dir(
    driver: CloudDrive,
    spec: TaskSpec,
    ref: ShareRef,
    magic_regex: dict | None,
    share_path: str,
    rel_path: str,
    share_list: list[FsItem],
    log: LogFn,
    result: TaskRunResult,
    plan_only: bool = False,
) -> None:
    """比对一个目录层。share_path 为分享内路径（驱动定位），rel_path 为对应保存目录后缀。"""
    mr = MagicRename(magic_regex)
    mr.set_taskname(spec.taskname)
    pattern, replace = mr.magic_regex_conv(spec.pattern, spec.replace)

    target_path = _norm_path(f"{spec.savepath}{rel_path}")
    if plan_only:
        # 试跑不建目录，所以列目录大概率会抛；按"列不出来=空目录=全部算新增"降级。
        # 真实运行那一侧仍走 ensure_dir + 裸 list_dir，一行都不改语义。
        dir_items = []
        try:
            dir_items = await driver.list_dir(target_path)
        except DriveError:
            pass
    else:
        await driver.ensure_dir(target_path)
        dir_items = await driver.list_dir(target_path)
    dir_names = [i.name for i in dir_items]

    need_save: list[_Plan] = []
    for share_file in share_list:
        # 过滤只影响「是否入选转存」，不影响 startfid 截断：即便 startfid 文件被过滤掉，
        # 也仍需在下方 break，避免越过起始点继续转更旧的文件。
        passes = share_file.is_dir or matches_filters(
            share_file.name, spec.episode_start, spec.episode_end, spec.quality
        )
        if not passes:  # 只多这一句计数：真实运行也计，dry-run 才有「被过滤 N 项」可说
            result.filtered_out += 1
        if passes:
            search_pattern = spec.update_subdir if (share_file.is_dir and spec.update_subdir) else pattern
            if re.search(search_pattern or "", share_file.name):
                if not mr.is_exists(share_file.name, dir_names, spec.ignore_extension and not share_file.is_dir):
                    if share_file.is_dir or rel_path:
                        # 文件夹、子目录文件不重命名
                        need_save.append(_Plan(share_file, share_file.name))
                    else:
                        name_re = mr.sub(pattern, replace, share_file.name)
                        if not mr.is_exists(name_re, dir_names, spec.ignore_extension):
                            need_save.append(_Plan(share_file, name_re))
                        else:
                            result.planned_existing += 1  # 改名后又撞名：计「已存在跳过」，不改既有跳过行为
                elif share_file.is_dir and spec.update_subdir and re.search(spec.update_subdir, share_file.name):
                    if spec.update_subdir_resave and driver.has("delete"):
                        log("info", f"重存子目录：{target_path}/{share_file.name}")
                        existing = next((i for i in dir_items if i.name == share_file.name and i.is_dir), None)
                        if existing and not plan_only:  # 试跑不删：把「会重存」如实落成一条计划目录
                            await driver.delete_items([existing], purge=True)
                            dir_names.remove(existing.name)
                            dir_items.remove(existing)
                        need_save.append(_Plan(share_file, share_file.name))
                    else:
                        # 递归模式：进入分享子目录比对
                        log("info", f"检查子目录：{share_file.name}")
                        sub_share_path = f"{share_path}/{share_file.name}"
                        before = len(result.files)
                        sub_items = await driver.list_share(ref, sub_share_path)
                        if sub_items:
                            await _check_dir(
                                driver,
                                spec,
                                ref,
                                magic_regex,
                                sub_share_path,
                                f"{rel_path}/{share_file.name}",
                                sub_items,
                                log,
                                result,
                                plan_only,
                            )
                        if len(result.files) > before:
                            log("info", f"子目录有新内容：{rel_path}/{share_file.name}")
                elif not share_file.is_dir:
                    # 原名即撞且非目录（pattern="" 或恒等 replace 的最常见任务）：第一次 is_exists 就命中，
                    # 真跑也是无声跳过——这里只补计数，不改任何控制流，否则界面的「已存在跳过 N 项」恒为 0。
                    result.planned_existing += 1
            # 目录在目标里已存在且没开递归：既有代码就是什么都不做，这里也不计 planned_existing
            # （它不是"跳过"，是"目录本身已在目标里、内容由递归或整目录搬走决定"——计了会让 UI 说谎）
            elif not share_file.is_dir and len(result.unmatched_samples) < UNMATCHED_SAMPLE_LIMIT:
                # FR-10：正则没命中 = 这个文件根本不会被转存。过去静默丢弃，试跑只说"0 个新增"，
                # 用户分不清是正则写错还是真没更新。带 20 个样本出去，一眼就能看出正则对不对。
                result.unmatched_samples.append(share_file.name)
        # 起始文件订阅：列表新→旧遍历，遇到 startfid（含）即停止（不受过滤影响）
        if share_file.fid == spec.startfid and spec.startfid:
            break

    if _I_RE.search(replace or ""):
        mr.set_dir_file_list(_mr_view(dir_items), replace)
        plans_view = [
            {"name_re": p.name_re, "mtime": p.item.mtime, "is_dir": p.item.is_dir} for p in need_save
        ]
        mr.sort_file_list(plans_view)
        for p, view in zip(need_save, plans_view, strict=True):
            p.name_re = view["name_re"]

    if not need_save:
        return

    if plan_only:
        # 试跑收口：判定全部在上面同一套代码里做完，这里只把计划条目如实落成结果——
        # 不转存、不重命名，new_fid 留空，dest_path 照常算；有任何计划条目即 updated。
        for plan in need_save:
            result.files.append(
                SavedFile(
                    share_name=plan.item.name,
                    final_name=plan.name_re,
                    new_fid="",
                    dest_path=_norm_path(f"{target_path}/{plan.name_re}"),
                    is_dir=plan.item.is_dir,
                    size=plan.item.size,
                )
            )
        result.status = "updated"
        return

    items = [p.item for p in need_save]
    save_result = await driver.save(items, target_path, ref)
    if not save_result.ok:
        raise DriveError(f"转存失败：{save_result.message}")
    if save_result.message:
        log("warn", save_result.message)

    if len(save_result.saved) == len(need_save):
        # 顺序一一对应，可安全重命名
        for plan, saved in zip(need_save, save_result.saved, strict=True):
            final_name = plan.name_re
            if (
                final_name != plan.item.name
                and not plan.item.is_dir
                and not rel_path
                and driver.has("rename")
            ):
                try:
                    await driver.rename(saved.fid, final_name)
                    log("info", f"重命名：{plan.item.name} → {final_name}")
                except DriveError as exc:
                    log("warn", f"重命名失败：{exc.message}")
            else:
                final_name = saved.name
            result.files.append(
                SavedFile(
                    share_name=plan.item.name,
                    final_name=final_name,
                    new_fid=saved.fid,
                    dest_path=_norm_path(f"{target_path}/{final_name}"),
                    is_dir=plan.item.is_dir,
                    size=plan.item.size,
                )
            )
    else:
        log("warn", f"转存结果数量（{len(save_result.saved)}）与计划（{len(need_save)}）不一致，跳过重命名")
        for saved in save_result.saved:
            result.files.append(
                SavedFile(
                    share_name=saved.name,
                    final_name=saved.name,
                    new_fid=saved.fid,
                    dest_path=_norm_path(f"{target_path}/{saved.name}"),
                    is_dir=saved.is_dir,
                    size=saved.size,
                )
            )
    result.status = "updated"
