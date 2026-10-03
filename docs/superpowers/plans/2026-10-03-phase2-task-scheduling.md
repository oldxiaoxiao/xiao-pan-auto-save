# 阶段2：任务级调度 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每个任务拥有独立的运行频率(如"每 5 分钟扫一次"),同时保证并发安全(转存串行、下载可并行)。

**Architecture:** 先加"并发运行锁"把 `run_tasks` 拆成锁内转存 + 锁外下载(P2-T1,安全前置);再给 `Task` 加 `schedule` 字段并让调度器为设了 schedule 的任务各注册一个 APScheduler job(P2-T2);前端加频率选择器(P2-T3)。未设 schedule 的任务仍走全局 crontab。

**Tech Stack:** FastAPI + SQLModel + APScheduler(AsyncIOScheduler, IntervalTrigger/CronTrigger);Vue3 + Element Plus;pytest。

**Spec:** `docs/superpowers/specs/2026-10-03-simplify-schedule-control-design.md`(阶段2 章节)

## Global Constraints

- 并发锁**先于**任务级 job:P2-T1 必须先落地,否则多 job 并发会撞 SQLite 写与账号频率。
- 转存阶段(写 DB、调网盘 save)在 `_run_lock` 内串行;下载阶段在锁外(允许重叠)。
- 向后兼容:`schedule=""` 的任务不单独注册,继续由全局 `MAIN_JOB_ID` crontab 驱动;`Task.schedule` 默认 `""`,旧行自动补默认(`_auto_add_columns`)。
- 保留全局 crontab 主 job,不删除。
- `interval:N` 的 N 下限 1 分钟;`cron:<expr>` 非法则不注册并记警告(不崩)。
- 每任务 job:`max_instances=1` + `coalesce=True` + `replace_existing=True`。
- 路由触发重注册用**函数内延迟导入** `from ..main import ...`(仿 `routes_settings.py:46` 避免循环依赖)。
- 用户可见文案中文。

---

### Task 1: 并发运行锁(转存串行 / 下载锁外)

**Files:**
- Modify: `backend/services/task_service.py`
- Test: `backend/tests/test_task_service.py`

**Interfaces:**
- Consumes: 现有 `run_tasks`/`_run_tasks_inner`/`_download_for_task`/`run_update_task`。
- Produces: 模块级 `_run_lock: asyncio.Lock`;`_run_tasks_inner` 每任务转存段在 `async with _run_lock:` 内、下载段在锁外。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_task_service.py` 追加(用 monkeypatch 让 `run_update_task` 记录进入/退出的重叠):
```python
import asyncio
import pytest
from backend.services import task_service as ts


@pytest.mark.asyncio
async def test_transfer_phase_serialized_across_calls(monkeypatch):
    active = {"n": 0, "max": 0}

    class FakeResult:
        status = "no_changes"; message = ""; files = []
        def render(self): return ""

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0.05)
        active["n"] -= 1
        return FakeResult()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    monkeypatch.setattr(ts, "load_tasks", lambda ids=None: [_mktask(1), _mktask(2)])
    monkeypatch.setattr(ts, "route_driver", lambda url: None)  # 无驱动→计 failed 但已过锁? 不,route 在锁内前
    # 直接并发两个 run_tasks,断言转存从不重叠(max==1)
    await asyncio.gather(ts.run_tasks([1], "scheduled"), ts.run_tasks([2], "scheduled"))
    assert active["max"] <= 1


def _mktask(tid):
    from backend.models import Task
    return Task(id=tid, taskname=f"t{tid}", shareurl="https://x/s", savepath="/s",
                auto_download=False, disabled=False)
```
注:若 `route_driver` 返回 None 会跳过转存段,测不到锁。改为 monkeypatch `route_driver` 返回一个 supported 的假类 + `_pick_account` 返回假账号,使流程真正进入 `run_update_task`。实现者按此调整 fixture,确保 fake_run_update 被调用。

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/pytest -q backend/tests/test_task_service.py::test_transfer_phase_serialized_across_calls -v`
Expected: FAIL(max>1,因当前无锁)

- [ ] **Step 3: 加锁并拆分转存/下载**

`task_service.py` 顶部 `import asyncio`;模块级 `_run_lock = asyncio.Lock()`。
`_run_tasks_inner` 每任务体改为:转存 + DB 写入包进 `async with _run_lock:`,下载移到锁外。具体:把
```python
        tlog("info", f"《{task.taskname}》开始运行")
        result = await run_update_task(driver, _task_spec(task), magic_regex=magic_regex, log=tlog)

        with session_scope() as session:
            row = session.get(Task, task.id)
            if row:
                row.last_run_at = datetime.now()
                if result.status == "banned":
                    row.shareurl_ban = result.message
                session.add(row)
```
替换为:
```python
        tlog("info", f"《{task.taskname}》开始运行")
        async with _run_lock:
            result = await run_update_task(driver, _task_spec(task), magic_regex=magic_regex, log=tlog)
            with session_scope() as session:
                row = session.get(Task, task.id)
                if row:
                    row.last_run_at = datetime.now()
                    if result.status == "banned":
                        row.shareurl_ban = result.message
                    session.add(row)
```
其后的 `if result.status == "updated": ... await _download_for_task(...)` 保持**缩进在锁外**(不包进 async with)。

- [ ] **Step 4: 运行测试通过 + 全量 + ruff**

Run: `.venv/bin/pytest -q backend/tests/test_task_service.py` → PASS;`.venv/bin/pytest -q` 全绿;`.venv/bin/ruff check backend` clean

- [ ] **Step 5: 提交**

```bash
git add backend/services/task_service.py backend/tests/test_task_service.py
git commit -m "feat(scheduler): 转存阶段加全局运行锁串行化，下载阶段锁外并行"
```

---

### Task 2: Task.schedule 字段 + 任务级 job 注册

**Files:**
- Modify: `backend/models.py`(Task)、`backend/schemas.py`(TaskIn)、`backend/core/scheduler.py`、`backend/main.py`、`backend/api/routes_tasks.py`、`frontend/src/api/types.ts`
- Test: `backend/tests/test_scheduler_log.py`、`backend/tests/test_api.py`

**Interfaces:**
- Consumes: `_run_lock`/`run_tasks`(T1);`scheduler` 单例(main)。
- Produces:
  - `Task.schedule: str = ""`、`TaskIn.schedule: str = ""`、`TaskPayload.schedule: string`。
  - `TaskScheduler.reschedule_task(task_id, schedule, func) -> str | None`、`TaskScheduler.unschedule_task(task_id) -> None`。
  - `main._run_one_task(task_id)`、`main.apply_task_schedule(task)`、`main.reschedule_all_tasks()`。

- [ ] **Step 1: 写失败测试(scheduler 解析)**

`backend/tests/test_scheduler_log.py` 追加:
```python
from backend.core.scheduler import TaskScheduler


def test_reschedule_task_interval_and_cron():
    s = TaskScheduler(); s.start()
    try:
        assert s.reschedule_task(1, "interval:5", lambda: None) is not None
        assert s.scheduler.get_job("xiao_pan_task_1") is not None
        assert s.reschedule_task(2, "cron:*/10 * * * *", lambda: None) is not None
        # 非法 cron → 不注册、不崩
        assert s.reschedule_task(3, "cron:not-a-cron", lambda: None) is None
        assert s.scheduler.get_job("xiao_pan_task_3") is None
        # 空 schedule → 撤销单独注册
        s.reschedule_task(1, "", lambda: None)
        assert s.scheduler.get_job("xiao_pan_task_1") is None
        s.unschedule_task(2)
        assert s.scheduler.get_job("xiao_pan_task_2") is None
    finally:
        s.shutdown()
```

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/pytest -q backend/tests/test_scheduler_log.py::test_reschedule_task_interval_and_cron -v`
Expected: FAIL(无 reschedule_task)

- [ ] **Step 3: 加字段**

`models.py` Task 内 `quality: str = ""` 下方加 `schedule: str = ""  # ""=继承全局；interval:N=每N分钟；cron:<expr>`。
`schemas.py` TaskIn 内 `quality: str = ""` 下方加 `schedule: str = ""`。
`types.ts` TaskPayload 内 `quality: string;` 下方加 `schedule: string;`。

- [ ] **Step 4: 实现 scheduler 方法**

`backend/core/scheduler.py` 顶部加 `from apscheduler.triggers.interval import IntervalTrigger`;`TaskScheduler` 内加:
```python
    def reschedule_task(self, task_id: int, schedule: str, func) -> str | None:
        job_id = f"xiao_pan_task_{task_id}"
        if not schedule:
            self.unschedule_task(task_id)
            return None
        trigger = self._parse_trigger(schedule)
        if trigger is None:
            return None
        self.scheduler.add_job(
            func, trigger=trigger, id=job_id, replace_existing=True, max_instances=1, coalesce=True
        )
        return str(trigger)

    def unschedule_task(self, task_id: int) -> None:
        job_id = f"xiao_pan_task_{task_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    @staticmethod
    def _parse_trigger(schedule: str):
        if schedule.startswith("interval:"):
            try:
                mins = int(schedule.split(":", 1)[1])
            except ValueError:
                return None
            return IntervalTrigger(minutes=max(1, mins))
        if schedule.startswith("cron:"):
            try:
                return CronTrigger.from_crontab(schedule.split(":", 1)[1])
            except (ValueError, KeyError, RuntimeError):
                return None
        return None
```

- [ ] **Step 5: main 入口 + 启动注册**

`backend/main.py`:
- 顶部 `from sqlmodel import select`(若无)。
- 加:
```python
async def _run_one_task(task_id: int) -> None:
    from .services import task_service
    try:
        await task_service.run_tasks(task_ids=[task_id], trigger="scheduled")
    except Exception as exc:  # noqa: BLE001
        hub.make_logger("scheduled")("error", f"任务 {task_id} 运行异常：{exc}")


def apply_task_schedule(task) -> None:
    from functools import partial
    from .database import session_scope
    from .models import Task
    with session_scope() as s:
        row = s.get(Task, task.id)
    if row is None or row.disabled:
        scheduler.unschedule_task(task.id)
        return
    scheduler.reschedule_task(task.id, getattr(row, "schedule", "") or "", partial(_run_one_task, task.id))


def reschedule_all_tasks() -> None:
    from .database import session_scope
    from .models import Task
    with session_scope() as s:
        tasks = s.exec(select(Task)).all()
    for t in tasks:
        apply_task_schedule(t)
```
- lifespan 内 `reschedule_main_job()` 之后加 `reschedule_all_tasks()`。

- [ ] **Step 6: routes_tasks 挂钩**

`backend/api/routes_tasks.py` 的 create_task / update_task / delete_task 成功返回前加(延迟导入避免循环):
- create/update 末尾:`from ..main import apply_task_schedule; apply_task_schedule(task)`
- delete 末尾(在 `with` 块外,拿到 task 后):`from ..main import scheduler; scheduler.unschedule_task(task_id)`

- [ ] **Step 7: 测试通过 + 全量 + ruff + typecheck**

Run: `.venv/bin/pytest -q backend/tests/test_scheduler_log.py backend/tests/test_api.py` → PASS;`.venv/bin/pytest -q` 全绿;`.venv/bin/ruff check backend` clean;`cd frontend && npm run typecheck`(TaskForm 未加 schedule 控件前,blank() 缺字段会报错 → 见 P2-T3;若报错记为已知,留 T3 收口)。

- [ ] **Step 8: 提交**

```bash
git add backend/models.py backend/schemas.py backend/core/scheduler.py backend/main.py backend/api/routes_tasks.py frontend/src/api/types.ts backend/tests/test_scheduler_log.py
git commit -m "feat(scheduler): Task.schedule 字段 + 每任务独立 APScheduler job 注册"
```

---

### Task 3: 频率选择器 UI

**Files:**
- Modify: `frontend/src/components/TaskForm.vue`
- Test: typecheck + build + 浏览器

**Interfaces:**
- Consumes: `TaskPayload.schedule`(P2-T2)。
- Produces: 基础区"更新频率"选择器,写回 `draft.schedule`。

- [ ] **Step 1: blank() 补字段**

`TaskForm.vue` `blank()` 内 `quality: "",` 下方加 `schedule: "",`。

- [ ] **Step 2: 频率选项 + 计算属性**

`<script setup>` 加:
```typescript
const SCHEDULE_PRESETS = [
  { label: "继承全局", value: "" },
  { label: "每 5 分钟", value: "interval:5" },
  { label: "每 30 分钟", value: "interval:30" },
  { label: "每小时", value: "interval:60" },
  { label: "每天 9:00", value: "cron:0 9 * * *" },
  { label: "自定义 cron", value: "__custom" },
];
const customCron = ref("");
const scheduleMode = computed({
  get: () => {
    const s = draft.schedule;
    if (s.startsWith("cron:") && !SCHEDULE_PRESETS.some((p) => p.value === s)) return "__custom";
    return s;
  },
  set: (v: string) => {
    if (v === "__custom") draft.schedule = customCron.value ? `cron:${customCron.value}` : "";
    else draft.schedule = v;
  },
});
watch(customCron, (c) => { if (scheduleMode.value === "__custom") draft.schedule = c ? `cron:${c}` : ""; });
```
（若编辑已有任务是自定义 cron,初始化 customCron:在 `loadFrom` 里 `if (task?.schedule?.startsWith("cron:") && !presets.includes) customCron.value = task.schedule.slice(5)`。）

- [ ] **Step 3: 基础区控件**

在"下载到本地"字段块之后加:
```vue
      <div class="f f--wide">
        <label class="field-label">更新频率</label>
        <el-select v-model="scheduleMode" style="width: 100%">
          <el-option v-for="p in SCHEDULE_PRESETS" :key="p.value" :label="p.label" :value="p.value" />
        </el-select>
        <el-input
          v-if="scheduleMode === '__custom'"
          v-model="customCron"
          placeholder="标准 crontab，如 */5 17-23 * * *"
          style="margin-top: 8px"
        />
        <div v-if="draft.schedule.startsWith('interval:') && Number(draft.schedule.split(':')[1]) < 5" class="hint">
          频率过高可能触发夸克风控，建议 ≥5 分钟。
        </div>
      </div>
```

- [ ] **Step 4: typecheck + build + 浏览器验证**

Run: `cd frontend && npm run typecheck && npm run build` → 通过。
浏览器:新建任务选"每 5 分钟"→保存→`GET /api/tasks` 该任务 `schedule=="interval:5"`;重启后端后该任务应注册为独立 job(`GET /api/scheduler` 或看日志);自定义 cron 输入生效。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/TaskForm.vue
git commit -m "feat(ui): 任务表单更新频率选择器(预设+自定义 cron)"
```

---

## Self-Review

- **Spec 覆盖**:schedule 字段(T2)、scheduler reschedule_task/unschedule_task(T2)、main _run_one_task+lifespan+routes 挂钩(T2)、_run_lock 转存串行/下载锁外(T1)、频率 UI + <5min 提示(T3)。全含。
- **占位符**:无 TBD;各步含代码。T1 测试 fixture 注明需 monkeypatch route_driver/_pick_account 使流程进入转存段(避免测不到锁)。
- **类型一致**:`schedule` 字段名贯穿 models/schemas/types/TaskForm;`reschedule_task(task_id, schedule, func)` 签名 T2 定义、main/routes 调用一致;job id 前缀 `xiao_pan_task_` 统一。
- **顺序**:T1(锁,安全前置)→ T2(字段+注册)→ T3(UI)。
