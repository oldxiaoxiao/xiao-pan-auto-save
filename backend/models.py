"""SQLModel 数据表定义。"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import NaiveDatetime
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


RUN_MODES: tuple[str, str, str] = ("follow", "manual", "once")  # 定时追更 / 仅手动 / 一次性


def run_mode_of(task) -> str:
    """读取执行形态：老库升级出来的空串与任何未知值一律按 follow（定时追更）。

    新列由 _auto_add_columns() 以 NOT NULL DEFAULT '' 补齐，存量行拿到的是空串而不是 "follow"；
    不归一化就会把用户的存量追更任务误判成"不自动跑"，静默停更。
    """
    mode = getattr(task, "run_mode", "") or ""
    return mode if mode in RUN_MODES else "follow"


class Account(SQLModel, table=True):
    __tablename__ = "account"

    id: int | None = Field(default=None, primary_key=True)
    driver_key: str = Field(index=True)
    name: str = ""  # 用户备注
    cookie: str = ""  # 完整 CK（可含 #kps=..&sign=..&vcode=.. 尾部）
    enabled: bool = True
    sort_order: int = 0
    nickname: str = ""
    capacity_used: int = 0
    capacity_total: int = 0
    member_type: str = ""
    can_save: bool = False
    last_check_at: NaiveDatetime | None = None
    last_sign_at: NaiveDatetime | None = None
    sign_message: str = ""


class Task(SQLModel, table=True):
    __tablename__ = "task"

    id: int | None = Field(default=None, primary_key=True)
    taskname: str
    shareurl: str
    savepath: str
    pattern: str = ""
    replace: str = ""
    ignore_extension: bool = False
    startfid: str = ""
    update_subdir: str = ""
    update_subdir_resave: bool = False
    enddate: str = ""  # YYYY-MM-DD，超期不运行
    runweek: str = "[]"  # JSON 数组 [1..7]，周一=1
    auto_download: bool = False  # 转存成功后下载到本地
    download_subdir: bool = False  # 递归下载转存的子目录
    download_savepath: str = ""  # 空=镜像网盘目录；非空=平铺到 下载根/该路径
    disabled: bool = False
    shareurl_ban: str = ""  # 非空 = 失效原因，永久跳过
    account_id: int | None = Field(default=None, foreign_key="account.id")
    sort_order: int = 0
    episode_start: int = 0  # 起始集（含），0=不限
    episode_end: int = 0  # 结束集（含），0=不限
    quality: str = ""  # 逗号分隔 token，如 "1080p,4k"，空=不限
    schedule: str = ""  # ""=继承全局；interval:N=每N分钟；cron:<expr>
    retry_attempts: int = 0  # 一次性任务本轮已消耗的重试次数（成功或手动再开时归零）
    next_retry_at: NaiveDatetime | None = None  # 到点重试时间；内存 JobStore 重启会丢，故必须落库
    run_mode: str = "follow"  # follow=定时追更 | manual=仅手动 | once=一次性（跑完自动停用）
    last_run_at: NaiveDatetime | None = None
    created_at: NaiveDatetime = Field(default_factory=_now)

    def runweek_list(self) -> list[int]:
        try:
            return [int(x) for x in json.loads(self.runweek or "[]")]
        except (ValueError, TypeError):
            return []


class DownloadRecord(SQLModel, table=True):
    """下载账本：一条 = 一次下载动作的完整生命周期（start 落 queued，finish 写终态）。"""

    __tablename__ = "download_record"

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(default="builtin", index=True)  # builtin | aria2
    ref_id: str = Field(default="", index=True)  # 内置=registry job_id；aria2=gid
    task_id: int | None = None  # 只存值不加外键：任务删除后历史仍要留
    taskname: str = ""
    filename: str = ""
    dest_path: str = ""
    size_total: int = 0
    size_done: int = 0
    fid: str = ""  # 网盘 fid，重下取直链用
    driver_key: str = ""
    account_id: int | None = None
    status: str = Field(default="queued", index=True)  # queued|downloading|done|failed|skipped|stopped
    error: str = ""
    created_at: NaiveDatetime = Field(default_factory=_now)
    finished_at: NaiveDatetime | None = None


class Setting(SQLModel, table=True):
    __tablename__ = "setting"

    key: str = Field(primary_key=True)
    value: str = "{}"  # JSON 序列化值

    def get(self) -> object:
        return json.loads(self.value or "{}")

    def set(self, obj: object) -> None:
        self.value = json.dumps(obj, ensure_ascii=False)


class ExternalApiToken(SQLModel, table=True):
    __tablename__ = "external_api_token"

    token: str = Field(primary_key=True)
    name: str = ""
    created_at: NaiveDatetime = Field(default_factory=_now)
