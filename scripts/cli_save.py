"""M1 自测 CLI：不依赖 Web，直接跑一个夸克转存任务。

用法：
  python scripts/cli_save.py --cookie-file ck.txt \
      --shareurl "https://pan.quark.cn/s/xxxx" --savepath "/动漫/测试" \
      --taskname "测试" [--pattern ... --replace ... --preview]

--preview 只列分享文件与目标目录现状，不做任何写入。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.engine import TaskSpec, run_update_task  # noqa: E402
from backend.core.router import make_driver  # noqa: E402
from backend.drivers.base import ShareBanned, ShareUnavailable  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="xiao-pan-auto-save 手动转存/追更")
    p.add_argument("--cookie", default="", help="夸克 Cookie（或从环境变量 QUARK_COOKIE 读取）")
    p.add_argument("--cookie-file", default="", help="从文件读取 Cookie")
    p.add_argument("--shareurl", required=True)
    p.add_argument("--savepath", required=True)
    p.add_argument("--taskname", default="CLI任务")
    p.add_argument("--pattern", default="")
    p.add_argument("--replace", default="")
    p.add_argument("--ignore-extension", action="store_true")
    p.add_argument("--startfid", default="")
    p.add_argument("--update-subdir", default="")
    p.add_argument("--update-subdir-resave", action="store_true")
    p.add_argument("--proxy", default=os.getenv("PROXY", ""))
    p.add_argument("--preview", action="store_true", help="仅预览分享列表与目标目录，不写入")
    return p.parse_args()


def log(level: str, msg: str) -> None:
    print(f"[{level:^5}] {msg}")


async def main() -> int:
    args = parse_args()
    cookie = args.cookie or os.getenv("QUARK_COOKIE", "")
    if args.cookie_file:
        cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip()
    if not cookie:
        log("error", "缺少 Cookie：--cookie / --cookie-file / 环境变量 QUARK_COOKIE")
        return 2

    driver = make_driver(args.shareurl, cookie, proxy=args.proxy)

    if args.preview:
        ref = driver.parse_share(args.shareurl)
        try:
            items = await driver.list_share(ref, "")
        except (ShareBanned, ShareUnavailable) as exc:
            log("error", f"分享不可用：{exc.message}")
            return 1
        log("info", f"分享根目录共 {len(items)} 项：")
        for i in items:
            icon = "📁" if i.is_dir else "📄"
            print(f"  {icon} {i.name}  (fid={i.fid} size={i.size})")
        existing = await driver.list_dir(args.savepath)
        log("info", f"目标目录 {args.savepath} 现有 {len(existing)} 项")
        await driver.close()
        return 0

    spec = TaskSpec(
        taskname=args.taskname,
        shareurl=args.shareurl,
        savepath=args.savepath,
        pattern=args.pattern,
        replace=args.replace,
        ignore_extension=args.ignore_extension,
        startfid=args.startfid,
        update_subdir=args.update_subdir,
        update_subdir_resave=args.update_subdir_resave,
    )
    result = await run_update_task(driver, spec, log=log)
    await driver.close()

    log("info", f"运行结果：{result.status} {result.message}")
    if result.files:
        print(result.render())
        return 0
    return 0 if result.status in ("no_changes", "network") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
