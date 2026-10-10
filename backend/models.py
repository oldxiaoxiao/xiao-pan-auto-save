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
    cookie: str = ""  # 已废弃：迁移到 cookie_enc 后恒为空，仅迁移期短暂有值
    cookie_enc: str = ""  # FR-08：加密后的 CK；明文不再落盘
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
    # FR-02：最近一次健康检查的结论。check_ok=False 即 Cookie 失效，
    # 界面打「需更新」标、任务不再空跑、并按 24h 去重推送一次提醒。
    check_ok: bool = True
    check_message: str = ""
    invalid_notified_at: NaiveDatetime | None = None


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
    auto_download: bool = False  # 转存成功后下载到本地；模型默认刻意保持 False（存量库 _auto_add_columns 补列语义依赖它），"默认开"只在 API 三条创建/更新入口生效，不是矛盾
    download_subdir: bool = False  # 递归下载转存的子目录
    download_savepath: str = ""  # 空=镜像网盘目录；非空=平铺到 下载根/该路径
    disabled: bool = False
    shareurl_ban: str = ""  # 非空 = 失效原因，永久跳过
    account_id: int | None = Field(default=None, foreign_key="account.id")
    # FR-06：绑定账号当下不可用时，允许切到同驱动的其它可用账号；关掉=严格只用绑定的那个号
    account_failover: bool = True
    sort_order: int = 0
    episode_start: int = 0  # 起始集（含），0=不限
    episode_end: int = 0  # 结束集（含），0=不限
    quality: str = ""  # 逗号分隔 token，如 "1080p,4k"，空=不限
    schedule: str = ""  # ""=继承全局；interval:N=每N分钟；cron:<expr>
    retry_attempts: int = 0  # 一次性任务本轮已消耗的重试次数（成功或手动再开时归零）
    next_retry_at: NaiveDatetime | None = None  # 到点重试时间；内存 JobStore 重启会丢，故必须落库
    run_mode: str = "follow"  # follow=定时追更 | manual=仅手动 | once=一次性（跑完自动停用）
    last_run_at: NaiveDatetime | None = None
    # FR-04：仅告知级通知（转存成功摘要）按任务维度可关；需处理级永远发，不受这个开关影响
    notify_info: bool = True
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


AI_PROVIDERS: tuple[str, str] = ("openai", "anthropic")  # openai=Chat Completions 兼容协议
AI_MODES: tuple[str, str] = ("chat", "copilot")  # chat=只读问答 | copilot=计划+确认
AI_ROLES: tuple[str, str, str, str] = ("user", "assistant", "tool", "system")


class TaskRun(SQLModel, table=True):
    """每次运行的结论账本（FR-03）：过去结果只活在日志里，算不出"连续失败"也给不出最近原因。"""

    __tablename__ = "task_run"

    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(index=True)
    run_id: str = ""
    status: str = ""  # updated|no_changes|banned|network|failed|quota|skipped
    message: str = ""
    created_at: NaiveDatetime = Field(default_factory=_now)


class DriveBackoff(SQLModel, table=True):
    """网盘账号的限流退避账本：连续触发风控就按指数退避暂停该账号，避免无限重试把号打死。"""

    __tablename__ = "drive_backoff"

    account_id: int = Field(primary_key=True)
    fail_count: int = 0
    until_at: NaiveDatetime | None = None  # 退避到期时间；为空或已过期即不生效
    reason: str = ""
    updated_at: NaiveDatetime = Field(default_factory=_now)


class NotifyPending(SQLModel, table=True):
    """免打扰时段的待发通知队列（FR-04）。

    「需处理」级在免打扰时段不允许被丢弃，只能攒到次日聚合成一条摘要——
    所以这里必须落库：进程重启、服务半夜被停，攒下的事件不能凭空消失。
    """

    __tablename__ = "notify_pending"

    id: int | None = Field(default=None, primary_key=True)
    level: str = Field(default="info", index=True)  # action=需处理 | info=仅告知
    title: str = ""
    body: str = ""
    task_id: int | None = None
    created_at: NaiveDatetime = Field(default_factory=_now)


class AiProvider(SQLModel, table=True):
    """AI 服务配置。api_key_enc 为加密后的 Key，明文永不落库。"""

    __tablename__ = "ai_provider"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(default="default", unique=True, index=True)  # 配置标识
    provider: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key_enc: str = ""
    temperature: float = 0.2
    timeout_ms: int = 30_000
    mode: str = "copilot"  # chat 模式下不生成任何动作卡
    monthly_token_limit: int = 0  # 万 token，0=不限
    enabled: bool = True
    tool_call: bool = True  # 能力声明：不支持工具调用时只能只读问答
    created_at: NaiveDatetime = Field(default_factory=_now)
    updated_at: NaiveDatetime = Field(default_factory=_now)


class AiChatSession(SQLModel, table=True):
    __tablename__ = "ai_chat_session"

    id: int | None = Field(default=None, primary_key=True)
    title: str = ""
    mode: str = "copilot"
    created_at: NaiveDatetime = Field(default_factory=_now)
    last_at: NaiveDatetime = Field(default_factory=_now)


class AiChatMessage(SQLModel, table=True):
    """一条消息 = 一次完整的轮次（含工具调用摘要、来源、待确认动作）。"""

    __tablename__ = "ai_chat_message"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(index=True)
    role: str = "user"
    content: str = ""
    sources: str = "[]"  # JSON
    actions: str = "[]"  # JSON：待确认/已执行的动作卡
    confidence: str = ""
    model: str = ""
    prompt_version: str = ""
    tools_version: str = ""
    feedback: str = ""  # 空 | up | down | action_confirmed | action_edited | action_cancelled
    error: str = ""
    created_at: NaiveDatetime = Field(default_factory=_now)


class AiUsage(SQLModel, table=True):
    """用量账本：每次模型调用一行，用于成本可见与预算控制（不外发）。"""

    __tablename__ = "ai_usage"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int | None = None
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    ok: bool = True
    error: str = ""
    created_at: NaiveDatetime = Field(default_factory=_now)
