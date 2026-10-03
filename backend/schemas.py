"""请求/响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TaskIn(BaseModel):
    taskname: str = Field(min_length=1)
    shareurl: str = Field(min_length=1)
    savepath: str = Field(min_length=1)
    pattern: str = ""
    replace: str = ""
    ignore_extension: bool = False
    startfid: str = ""
    update_subdir: str = ""
    update_subdir_resave: bool = False
    enddate: str = ""
    runweek: list[int] = []
    auto_download: bool = False
    download_subdir: bool = False
    download_savepath: str = ""
    disabled: bool = False
    account_id: int | None = None
    sort_order: int = 0
    episode_start: int = 0
    episode_end: int = 0
    quality: str = ""


class TaskOut(TaskIn):
    id: int
    shareurl_ban: str = ""
    last_run_at: str | None = None


class AccountIn(BaseModel):
    name: str = ""
    driver_key: str = "quark"
    cookie: str = ""  # 更新时可留空表示不改动；创建时必填
    enabled: bool = True
    sort_order: int = 0


class AccountOut(BaseModel):
    id: int
    name: str
    driver_key: str
    enabled: bool
    sort_order: int
    nickname: str = ""
    member_type: str = ""
    capacity_used: int = 0
    capacity_total: int = 0
    can_save: bool = False
    last_check_at: str | None = None
    last_sign_at: str | None = None
    sign_message: str = ""
    cookie_masked: str = ""
