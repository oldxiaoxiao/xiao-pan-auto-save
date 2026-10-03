"""网盘驱动抽象层：统一数据结构 + CloudDrive 接口。

引擎层（backend/core/engine.py）只允许调用这里定义的接口，
任何网盘专有细节（token 体系、风控参数等）必须封装在驱动实现内部。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar


class DriveError(Exception):
    """驱动通用错误；retryable=True 表示引擎可视为临时网络问题。"""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class ShareBanned(DriveError):
    """分享链接已失效/被取消，任务应永久跳过并通知。"""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


class ShareUnavailable(DriveError):
    """网络异常等临时故障，本次跳过、下次重试，不记录失效。"""

    def __init__(self, message: str = "网络异常"):
        super().__init__(message, retryable=True)


class CapabilityError(DriveError):
    """驱动未声明该能力。"""

    def __init__(self, driver_key: str, capability: str):
        super().__init__(f"驱动 {driver_key} 不支持能力: {capability}")


@dataclass
class ShareRef:
    """parse_share 的产物：分享链接的定位信息。extra 承载驱动私有状态（如 stoken 缓存）。"""

    url: str
    pwd_id: str = ""
    passcode: str = ""
    pdir_fid: str = "0"  # 深链指定的分享内起始目录 fid（"0"/空表示根）
    sub_names: list[str] = field(default_factory=list)  # 深链目录名，仅展示用
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class FsItem:
    """统一文件条目。token 承载驱动私有凭证（夸克 = share_fid_token）。"""

    fid: str
    name: str
    is_dir: bool = False
    size: int = 0
    mtime: float = 0  # 更新时间戳（秒），分享列表排序依据
    token: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SaveResult:
    ok: bool
    saved: list[FsItem] = field(default_factory=list)  # 转存后的新位置条目，顺序与入参一致
    message: str = ""


@dataclass
class SignResult:
    ok: bool
    message: str = ""
    reward_bytes: int = 0


@dataclass
class AccountInfo:
    nickname: str = ""
    used: int = 0
    total: int = 0
    member_type: str = ""
    valid: bool = False  # Cookie 是否仍有效
    can_save: bool = False  # 是否具备转存权限（夸克：CK 含 __uid）


class CloudDrive(ABC):
    """所有网盘驱动基类。supported=False 的骨架驱动方法一律抛 NotImplementedError。"""

    key: ClassVar[str] = ""
    name: ClassVar[str] = ""
    share_domains: ClassVar[list[str]] = []
    supported: ClassVar[bool] = False
    capability: ClassVar[set[str]] = set()

    def __init__(self, cookie: str = "", proxy: str = "", index: int = 0):
        self.cookie = (cookie or "").strip()
        self.proxy = proxy
        self.index = index + 1  # 账号序号，从 1 开始，用于日志

    # ------------------------------------------------------------------
    # 能力与路由
    # ------------------------------------------------------------------
    def has(self, cap: str) -> bool:
        return self.supported and cap in self.capability

    def require(self, cap: str) -> None:
        if not self.has(cap):
            raise CapabilityError(self.key, cap)

    @classmethod
    def matches_share_url(cls, url: str) -> bool:
        return any(domain in url for domain in cls.share_domains)

    # ------------------------------------------------------------------
    # 追更五原语（已实现驱动必须提供）
    # ------------------------------------------------------------------
    @abstractmethod
    def parse_share(self, url: str) -> ShareRef: ...

    @abstractmethod
    async def list_share(self, ref: ShareRef, path: str = "") -> list[FsItem]:
        """列分享内某子路径的文件。path 为分享内路径（"" = 根/深链起点）。

        首次调用需换取 stoken；失效抛 ShareBanned，网络异常抛 ShareUnavailable。
        """

    @abstractmethod
    async def list_dir(self, path: str) -> list[FsItem]:
        """列自己网盘目录（path 绝对路径），目录不存在返回空列表。"""

    @abstractmethod
    async def save(self, items: list[FsItem], dest_path: str, ref: ShareRef | None = None) -> SaveResult:
        """把分享条目转存到自己网盘的 dest_path。

        items 须携带 token；ref 为条目来源的分享（驱动可能需要 pwd_id/stoken 等上下文）。
        """

    @abstractmethod
    async def ensure_dir(self, path: str) -> str:
        """确保目录存在，返回其 fid。"""

    # ------------------------------------------------------------------
    # 可选能力：未声明即不可调用
    # ------------------------------------------------------------------
    async def rename(self, item_or_fid: FsItem | str, new_name: str) -> None:
        self.require("rename")
        raise NotImplementedError

    async def delete_items(self, items: list[FsItem], purge: bool = True) -> None:
        """删除（移入回收站）；purge=True 时彻底删除，用于子目录重存模式。"""
        self.require("delete")
        raise NotImplementedError

    async def sign(self) -> SignResult:
        self.require("sign")
        raise NotImplementedError

    async def get_download_urls(self, fids: list[str]) -> tuple[list[dict], str]:
        """取文件直链：返回 ([{fid, file_name, size, download_url}, ...], 附加Cookie)。

        直链请求通常需要携带返回的 cookie_str 与驱动 UA，由下载器原样透传。
        """
        self.require("download")
        raise NotImplementedError

    async def list_dir_children(self, fid: str) -> list[FsItem]:
        """按目录 fid 列子项（下载递归子目录用）。"""
        raise DriveError(f"{self.name} 驱动不支持按 fid 列目录")

    async def account_info(self) -> AccountInfo:
        self.require("account")
        raise NotImplementedError

    async def close(self) -> None:  # noqa: B027  默认无资源可释放
        """释放 HTTP 资源。"""


class UnsupportedDrive(CloudDrive):
    """骨架驱动基类：supported=False，任何操作抛 NotImplementedError。

    子类只需声明 key/name/share_domains 和 `notes`（接入要点），
    并在文件头用 `# TODO(driver):` 标注实现该网盘的关键步骤。
    """

    notes: ClassVar[str] = ""
    capability: ClassVar[set[str]] = set()

    def _todo(self) -> NotImplementedError:
        return NotImplementedError(f"{self.name} 驱动尚未实现。接入要点：{self.notes}")

    def parse_share(self, url: str) -> ShareRef:
        raise self._todo()

    async def list_share(self, ref: ShareRef, path: str = "") -> list[FsItem]:
        raise self._todo()

    async def list_dir(self, path: str) -> list[FsItem]:
        raise self._todo()

    async def save(self, items: list[FsItem], dest_path: str, ref: ShareRef | None = None) -> SaveResult:
        raise self._todo()

    async def ensure_dir(self, path: str) -> str:
        raise self._todo()
