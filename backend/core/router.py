"""任务路由：shareurl 域名 → 驱动类 → 驱动实例。任务表不存网盘字段。"""

from __future__ import annotations

from ..drivers import route_driver
from ..drivers.base import CloudDrive, DriveError


def driver_class_for_share(shareurl: str) -> type[CloudDrive]:
    cls = route_driver(shareurl)
    if cls is None:
        raise DriveError(f"没有支持 {shareurl} 的网盘驱动")
    return cls


def make_driver(shareurl: str, cookie: str, proxy: str = "", index: int = 0) -> CloudDrive:
    return driver_class_for_share(shareurl)(cookie=cookie, proxy=proxy, index=index)
