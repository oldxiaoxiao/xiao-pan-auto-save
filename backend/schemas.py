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
    auto_download: bool = True  # 默认下载到本地：POST/PUT /api/tasks 与 /api/add_task（含 /api/v1/task/add）三条路径一致为开；显式传 false 才关
    download_subdir: bool = False
    download_savepath: str = ""
    disabled: bool = False
    account_id: int | None = None
    # FR-06：绑定账号不可用时是否允许切到同驱动的其它可用账号（关掉=严格只用绑定账号）
    account_failover: bool = True
    sort_order: int = 0
    episode_start: int = 0
    episode_end: int = 0
    quality: str = ""
    schedule: str = ""
    run_mode: str = "follow"
    # FR-04：仅告知级通知（转存成功摘要）开关；需处理级不受它影响
    notify_info: bool = True


class TaskOut(TaskIn):
    id: int
    shareurl_ban: str = ""
    last_run_at: str | None = None
    retry_attempts: int = 0
    next_retry_at: str | None = None
    health: dict = {}  # FR-03：{status, reason, fail_streak, last_status, kind}


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
    check_ok: bool = True
    check_message: str = ""
