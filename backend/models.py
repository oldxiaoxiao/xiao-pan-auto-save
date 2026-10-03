"""SQLModel 数据表定义。"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import NaiveDatetime
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


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
    last_run_at: NaiveDatetime | None = None
    created_at: NaiveDatetime = Field(default_factory=_now)

    def runweek_list(self) -> list[int]:
        try:
            return [int(x) for x in json.loads(self.runweek or "[]")]
        except (ValueError, TypeError):
            return []


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
