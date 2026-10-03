# 任务简化 + 调度频率 + 下载控制 设计（三阶段）

**Goal:** 让"下载某剧某几集某画质"变成填几个基础字段就能做的事,支持任务级"每 N 分钟扫一次"的高频追更,并让下载列表从只读变为可操作(停止/暂停/继续/删除)。

**Architecture:** 三个独立可交付阶段,按价值/风险排序:阶段1 表单简化 + 集数/画质一等公民字段(纯 UI + 引擎后置过滤,向后兼容);阶段2 任务级调度(每任务独立 APScheduler job);阶段3 下载操作(按内置/aria2 模式差异化)。每阶段各自 spec→plan→实现→真机验证。

**Tech Stack:** FastAPI + SQLModel + APScheduler(后端);Vue3 + TS + Element Plus(前端);httpx 内置下载器 + aria2 JSON-RPC。

**Spec 依据:** 本文档即三阶段的统一设计。分期实现时,阶段2/3 若体量增长可各自再出子 spec,但接口以本文为准。

## Global Constraints

- 向后兼容:新增 Task 字段一律带默认值(0 / "" / []),旧数据与旧行为不受影响;`episode_start=episode_end=0` 且 `quality=""` 时,引擎过滤逻辑与改造前完全一致。
- 不破坏现有 `pattern`/`replace`/`$TV` 正则机制:集数/画质是叠加在其上的**后置过滤**,不替换正则。
- 测试隔离:沿用 `backend/tests/conftest.py` 的临时 `DATA_DIR`,pytest 不得触碰真实 `data/xiao_pan.db`。
- 调度改动必须保留全局 crontab 作为"未设任务级频率"时的默认,不能删除全局 job。
- 前端"默认大于配置":基础表单只暴露高频字段,个性化字段折叠进默认收起的高级区,不删除任何字段。
- 语言:所有用户可见文案用中文。
- 频率下限:任务级间隔 < 1 分钟时仅提示夸克风控风险,不强制阻止。

---

## 阶段 1 — 表单简化 + 集数/画质一等公民字段

### 数据模型

`backend/models.py` 的 `Task` 新增:
```python
episode_start: int = 0   # 起始集（含），0 = 不限
episode_end: int = 0     # 结束集（含），0 = 不限
quality: str = ""        # 逗号分隔 token，如 "1080p,4k"；空 = 不限
```
`backend/schemas.py` 的 `TaskIn` 同步新增 `episode_start:int=0`、`episode_end:int=0`、`quality:str=""`。`TaskOut` 继承 `TaskIn` 自动获得。

`frontend/src/api/types.ts` 的 `TaskPayload` 新增同名三字段。

### 后置过滤（引擎）

新增纯函数于 `backend/core/engine.py`（其内调用 `MagicRename` 提取集数）：
```python
def matches_filters(name: str, ep_start: int, ep_end: int, quality: str, magic: MagicRename) -> bool:
    """画质：文件名含所选 token 之一（不区分大小写）才通过；quality 空则不限。
    集数：设了区间时用 magic 提取 {E} 集数，落在 [start,end] 才通过；
    设了区间但提不出集数 → 不通过；区间未设（两者皆 0）→ 通过。"""
```
- 画质 token 匹配:对 `name.lower()`,检查是否包含 `quality` 拆分出的任一 token(如 `4k`→也匹配 `2160p`? 不,严格按用户所填 token 子串匹配;UI 侧提供 `4K/2160P`、`1080P`、`720P`、`x265`、`HDR` 等预设,写入时归一为小写)。多选时任一命中即通过(OR)。
- 集数:复用 `MagicRename` 现有 `{E}` 候选正则提取集数整数;无法提取视为不匹配。

接入点:`backend/core/engine.py` 文件选中循环里,在现有 `re.search(search_pattern, name)` 命中之后、执行转存之前,追加 `if not matches_filters(...): continue`。对目录型 `update_subdir` 分支同样在叶子文件上应用。

### 前端表单两层

`frontend/src/components/TaskForm.vue` 重构:
- **基础区(默认可见)**:任务名/智能搜索、分享链接、保存路径、集数范围(`episode_start`/`episode_end` 两个 `el-input-number`,占位"不限")、画质(`el-select` 多选,预设 token)、下载到本地(`auto_download` 开关)、更新频率(阶段1 先占位隐藏,阶段2 接入)。
- **高级区(`el-collapse` 或抽屉,默认收起)**:pattern、replace、魔法变量、ignore_extension、startfid、update_subdir、update_subdir_resave、download_subdir、download_savepath、enddate、指定账号、runweek、disabled、sort_order。
- `blank()` 默认值补 `episode_start:0, episode_end:0, quality:""`;`snapshot()`/提交逻辑不变(全字段仍提交,只是显示分层)。

### 测试

- `test_magic.py`/`test_engine.py`:`matches_filters` 单测——画质命中/不命中/多 token OR/空;集数区间内/外/边界/提不出集数;两者皆空=全通过(回归)。
- `test_api.py`:创建任务带 `episode_start=1,episode_end=5,quality="1080p"` 能存能读回;默认值向后兼容。
- 前端:手动/构建校验基础表单只显高频字段、高级区可展开、提交 payload 含新字段。

---

## 阶段 2 — 任务级调度

### 数据模型

`Task` 新增:
```python
schedule: str = ""  # "" = 继承全局 crontab；"interval:5" = 每5分钟；"cron:<expr>" = 自定义
```
`TaskIn`/`TaskPayload` 同步新增 `schedule: str = ""`。

### 调度器改造

`backend/core/scheduler.py`:
- 保留 `MAIN_JOB_ID` 全局 job(驱动未设 `schedule` 的任务)。
- 新增 `reschedule_task(task_id, schedule, func)`:解析 `schedule` →
  - `interval:N` → `IntervalTrigger(minutes=N)`
  - `cron:<expr>` → `CronTrigger.from_crontab(expr)`,非法回退不注册并记警告
  - 空 → 不为该任务单独注册(走全局)
  - `add_job(func, trigger, id=f"xiao_pan_task_{id}", replace_existing=True, max_instances=1, coalesce=True)`
- 新增 `unschedule_task(task_id)`。

`backend/main.py`:
- 新增 `_run_one_task(task_id)` 入口(调用 `task_service.run_tasks(task_ids=[id], trigger="scheduled")`,内部仍过 `task_due_today`)。
- lifespan 启动时:遍历启用任务,对设了 `schedule` 的逐个 `reschedule_task`。
- `routes_tasks` 的 create/update/delete 后调用 `reschedule_task`/`unschedule_task`(update 若清空 schedule 则撤销该任务 job 回落到全局)。

### 频率选择器 UI

`TaskForm.vue` 基础区"更新频率":`el-select` 预设(每5分钟 / 每30分钟 / 每小时 / 每天9:00 / 继承全局 / 自定义) + 选"自定义"时出现 cron 输入框。写回 `schedule` 字段。间隔 <1 分钟(即 `interval:` 无法表达,或自定义 cron 每秒)时 `el-text` 提示夸克风控风险。

### 测试

- `test_scheduler_log.py`:`reschedule_task` 对 `interval:5`/`cron:...`/空 分别注册正确 trigger;非法 cron 不崩;`unschedule_task` 幂等。
- `test_api.py`:update 设置/清空 `schedule` 会触发(或不触发)重注册(可 monkeypatch scheduler 断言调用)。
- 真机:建一个 `interval:1` 的测试任务,观察日志按分钟级触发(用临时 DATA_DIR 或可清理的任务)。

---

## 阶段 3 — 下载操作（按模式差异化）

### job 结构补充

`DownloadRegistry.snapshot()` 每个内置 job dict 增加 `"source": "builtin"`;`aria2_status()` 每个 job 增加 `"source": "aria2"`。内置 `status` 增加 `"stopped"` 取值。

### 取消机制（内置）

`backend/core/download_registry.py`:
- 维护模块级 `_controls: dict[str, asyncio.Event]`(不进 `asdict`,不序列化)。
- `create()` 时建一个未置位的 Event;`stop(job_id)` 置位;`_fetch_one` 每个 chunk 循环检查该 Event,置位则中断流、删除 `.part`、`registry.update(status="stopped")` 并返回取消摘要。
- 任务结束/清理时移除对应 Event。

`backend/services/download_service.py` 的 `_fetch_one`:chunk 循环内 `if _controls.get(job_id) and _controls[job_id].is_set(): <cleanup + return>`。

### 控制端点

`backend/api/routes_downloads.py` 新增:
```
POST   /api/downloads/{id}/stop     # builtin: 置取消位; aria2: aria2.remove(gid)
POST   /api/downloads/{id}/pause    # 仅 aria2: aria2.pause(gid); builtin 返回 400 "内置下载不支持暂停"
POST   /api/downloads/{id}/resume   # 仅 aria2: aria2.unpause(gid); builtin 返回 400
DELETE /api/downloads/{id}          # builtin: 从 registry 移除记录(不删已下文件); aria2: aria2.removeDownloadResult(gid)
```
- 路由按 id 前缀/来源判定 builtin vs aria2:内置 id 是 uuid-hex,aria2 id 是 gid;更稳妥是快照里带 `source`,端点入参带 `?source=` 或路由分两组。采用**入参 `source` 查询参数**(`?source=builtin|aria2`),前端从 job 已知 source。
- aria2 操作复用 `DownloadSettings.from_dict(get_setting("download"))` 拿 RPC 地址/secret,发对应 JSON-RPC;不可达返回 502 友好提示(复用 `_drive_http` 风格)。

### 前端 DownloadsView

`frontend/src/views/DownloadsView.vue` 每行加操作列:
- `source==="aria2"` 且 status∈{downloading,queued} → 显示 暂停/继续/停止/删除。
- `source==="builtin"` 且 status==="downloading" → 显示 停止/删除。
- 已结束(done/failed/stopped/skipped) → 仅 删除(移出列表)。
- 点击调对应端点后 `refresh()` 快照。aria2 暂停项 status 变 `paused`(映射自 aria2),前端文案"已暂停"。

### 测试

- `test_download_registry.py`:`stop()` 置位 Event;`_fetch_one` 检测到取消位后删 `.part`、status=stopped(用可控 FakeClient 流)。
- `test_download_api.py`:内置 `pause`/`resume` 返回 400;`stop`/`DELETE` 正常;aria2 端点 monkeypatch RPC 断言发出的 method。
- 真机:aria2 容器内投递一个大文件,UI 点暂停→继续→停止,确认 RPC 生效、列表状态变化。

---

## 分期交付顺序

1. **阶段1** 先行:价值最高、风险最低,纯增量(新字段默认值 + 后置过滤 + 表单分层)。
2. **阶段2**:调度内核改动,依赖阶段1 已扩好的 Task 模型加 `schedule`。
3. **阶段3**:最复杂,动下载循环 + aria2 RPC,独立于 1/2 但放最后降低叠加风险。

每期完成后:pytest 全绿 + ruff 干净 + 后端重启真机验证 + 逻辑提交。推送需用户交互批准。
