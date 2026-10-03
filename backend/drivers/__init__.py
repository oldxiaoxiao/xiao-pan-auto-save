"""驱动注册表：自动发现 backend/drivers/ 下所有模块中的 CloudDrive 子类。"""

from __future__ import annotations

import importlib
import pkgutil

from .base import (
    AccountInfo,
    CapabilityError,
    CloudDrive,
    DriveError,
    FsItem,
    SaveResult,
    ShareBanned,
    ShareRef,
    ShareUnavailable,
    SignResult,
)
from .http import DriveHttpClient

DRIVERS: dict[str, type[CloudDrive]] = {}


def _discover() -> None:
    for mod_info in pkgutil.iter_modules(__path__):
        if mod_info.name in ("base", "http", "__init__"):
            continue
        module = importlib.import_module(f"{__name__}.{mod_info.name}")
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, CloudDrive)
                and attr is not CloudDrive
                and getattr(attr, "key", "")
            ):
                DRIVERS[attr.key] = attr


_discover()


def get_driver_class(key: str) -> type[CloudDrive] | None:
    return DRIVERS.get(key)


def route_driver(share_url: str) -> type[CloudDrive] | None:
    """按分享链接域名路由驱动，任务表不存网盘字段。"""
    for cls in DRIVERS.values():
        if cls.share_domains and cls.matches_share_url(share_url):
            return cls
    return None


def driver_matrix() -> list[dict]:
    """驱动支持矩阵，供 WebUI/CLI 展示。"""
    return [
        {
            "key": cls.key,
            "name": cls.name,
            "supported": cls.supported,
            "capability": sorted(cls.capability),
            "share_domains": cls.share_domains,
        }
        for cls in sorted(DRIVERS.values(), key=lambda c: (not c.supported, c.key))
    ]


__all__ = [
    "AccountInfo",
    "CapabilityError",
    "CloudDrive",
    "DRIVERS",
    "DriveError",
    "DriveHttpClient",
    "FsItem",
    "SaveResult",
    "ShareBanned",
    "ShareRef",
    "ShareUnavailable",
    "SignResult",
    "driver_matrix",
    "get_driver_class",
    "route_driver",
]
