# 任务执行形态（定时追更 / 仅手动 / 一次性）— 设计文档

- 日期：2026-10-05
- 状态：待评审
- 范围：给任务增加"执行形态"三态，一次性任务跑完自动停用；顺带修正批量「立即运行」对停用任务的处理
- 不含：新建任务流程的整体体验重构（另立 spec）

## 1. 目标

用户的原话是需求来源：**"不是每个任务都需要定时执行，因为有些任务是下载已经存在的资源，只需要执行一次，不需要定时追更。"**

1. 任务分三种执行形态：`定时追更` / `仅手动` / `一次性`，在表单第一屏一眼可选、在列表一眼可辨。
2. `仅手动`、`一次性` 永不自动触发（不进任务级定时器，也不被全局扫周期捞走），只能由用户点「运行」或「立即运行」触发。
3. `一次性` 任务在真正跑完后自动停用，并在列表上显示为「已完成」，不再占用注意力。
4. 顺带修正：全局「立即运行」目前会连**停用**的任务一起跑，语义上"暂停一切"应该真暂停。

## 2. 非目标（本期不做，YAGNI）

- **不做"归档/隐藏"**：`一次性` 跑完只停用，不从列表消失。隐藏会引入第三种可见状态和一层筛选，找回成本高于收益。
- **不新增第四种状态字段**：「已完成」= `run_mode=once` 且 `disabled` 的**派生显示**，不额外存标记位。
- **不自动替用户取消停用**：把 `一次性` 改回 `定时追更` 时不自动打开停用开关（猜用户意图是错的来源），只在 UI 上提醒。
- **不动 `schedule` 的既有取值语义**（`""` / `interval:N` / `cron:<expr>`），只让它在本形态不是 `定时追更` 时不参与注册。
- **不改字段布局、不重排表单**：属于 C 项（新建流程体验）。
- **不引入"失败自动重试"或定时重跑一次性任务**：补下失败文件走已上线的下载账本重下。

## 3. 现状（为什么现在做不到）

- 任务只有一个布尔 `disabled`（`backend/models.py` 的 `Task.disabled`）和一个字符串 `schedule`。
- 「停用」藏在表单默认收起的高级区（`frontend/src/components/TaskForm.vue:348-351`），第一屏看不见。
- `schedule` 填非法值**不会**变成"仅手动"：`TaskScheduler.reschedule_task`（`backend/core/scheduler.py:40-54`）解析失败时撤销作业并只打一条 warning，而 `has_valid_schedule` 为假的任务会**继续被全局扫周期捞走**（`backend/services/task_service.py:125-132`），于是每天 9:00 照跑。
- `disabled=1` 事实上是唯一的"不定时"手段，但全局「立即运行」（`POST /api/tasks/run`，`trigger="manual"`）**不看 `disabled`**——所有 `task_due_today`/形态判断都只在 `trigger=="scheduled"` 分支里生效（`task_service.py:118-136`），所以停用的任务照样被批量跑掉，列表上只留一个「停用」徽标。
- 下载结果没有结构化出口：`_download_for_task`（`task_service.py:190-213`）只把成功数拼进通知文案（`ok = sum(...)`）就丢弃，运行汇总里拿不到"这次下载有没有失败"，因此无法据此判断一次性任务是否真的跑完。

## 4. 设计

### 4.1 数据模型

`Task` 新增一列：

```python
run_mode: str = "follow"  # follow=定时追更 | manual=仅手动 | once=一次性
```

- 取值三选，**非法值后端 400 并报中文原因**（吸取 `schedule` 静默回退的教训：这次不让它悄悄生效）。
- 存量行迁移由 `backend/database.py:26` 的 `_auto_add_columns()` 补列，默认 `follow` —— **现有任务行为完全不变**。
- `run_mode` 与 `disabled` 正交，一句话写进 README：**执行方式决定要不要自动跑，停用决定要不要临时暂停一切**。
- 与 `enddate`（截止日期）、`runweek`（按星期）的关系：这三个只在 `run_mode=follow` 时有意义；非 `follow` 时表单收起、后端不参与判定，但**不清空已有值**（用户切回 `follow` 时期望原来的配置还在）。

### 4.2 调度层：谁会被自动跑

> 已被 `docs/superpowers/specs/2026-10-05-once-retry-budget-design.md` 修订：`once` 不再"不被任何自动触发驱动"，现行规则见那份的 4.3。以下为原文，保留不改写。

判定集中在一个函数里，避免多处漂移：

```python
# task_service.py 扫周期过滤链（现有 118-136 的位置）追加一环：
if trigger == "scheduled" and task.run_mode != "follow":
    continue  # 仅手动 / 一次性 不参与任何自动触发
```

- `backend/main.py:78-104`（`apply_task_schedule` / `_run_one_task`）：`run_mode != "follow"` 时一律 `unschedule_task`，不注册作业。
- 保存任务（`PUT /api/tasks/{id}`）与创建（`POST /api/tasks`）后必须调 `apply_task_schedule` **重新同步**：从 `follow` 改成 `manual/once` 要撤销已注册作业（否则旧定时器还会触发），反向则注册。这与刚上线的排序端点刻意不同——排序不改任何调度输入，所以那边不需要同步。
- 手动入口不变：`POST /api/tasks/{id}/run` 和 `POST /api/tasks/run` 走 `trigger="manual"`，不受形态限制（`仅手动` 的存在意义就是只从这儿走）。

### 4.3 一次性任务的收口判定

> 已被 `docs/superpowers/specs/2026-10-05-once-retry-budget-design.md` 修订：本表"未完成 → 保持启用"之后还要分岔——没放出不占重试预算、真失败吃一次预算并在三次用尽后停摆，现行判定见那份的 4.2；本表与下文提到的几条日志文案也已换成那份 4.7 列出的五条逐字文案。以下为原文，保留不改写。

放在运行收尾处，依赖 4.5 的结构化结果。**完成条件**（用户选定"新增+下载全成功才算完"）：

| 本次运行结果 | 开了下载到本地 | 判定 | 动作 |
| --- | --- | --- | --- |
| `updated`（有新增转存项） | 否 | 完成 | `disabled=1` + 日志「一次性任务已完成并自动停用」 |
| `updated` 且下载失败数 0（含全部 `skipped` 已存在） | 是 | 完成 | 同上 |
| `updated` 但下载有失败 | 是 | 未完成 | 保持启用，日志提示「N 项下载失败，请到下载页重下」 |
| `no_changes`（分享还没放资源） | 任意 | 未完成 | 保持启用，日志「本次无新增，一次性任务仍保持待执行」 |
| 下载实际未执行（驱动不支持下载 / 待下载清单为空） | 是 | 未完成 | 保持启用，日志说明原因 —— 防止"看着完成了其实没下" |
| `failed` / `banned` / `network` | 任意 | 未完成 | 保持启用 |

- 已停用（已完成）的 `once` 任务再手动跑一次：仍然跑，跑完**幂等**——不重复写 `disabled`、不重复发通知。
- 已知显示瑕疵：用户手动停用的 `once` 任务也会显示成「已完成」（派生规则无法区分停用来源）。本期接受，不引入额外标记位。

### 4.4 批量「立即运行」语义修正

`POST /api/tasks/run`（全局）与 `_run_tasks_inner`：

- **跳过 `disabled`**（含一次性已完成）——"暂停"就该真暂停；
- `仅手动` / `一次性`且未停用 → **参与**（批量点击本身是用户主动触发）；
- 单行「▶ 运行」按钮**始终可用**，包括 `disabled` 行，用于手工重试和验证；
- 汇总里新增跳过计数，通知文案写明「跳过 N 个已停用任务」，避免用户以为漏跑了。

### 4.5 下载结果结构化（本期唯一的既有代码改造）

`_download_for_task` 现在把结果吞成文案。改为返回结构化计数，供 4.3 判定与通知复用：

```python
# 返回 (新增项数, 下载尝试数, 下载成功数, 下载失败数, 下载是否实际执行)
counts = await _download_for_task(driver, task, result, settings, notify_lines, tlog, account_id=account.id)
```

- 成功/失败沿用现有前缀约定（`✅` / `❌` 开头，通知聚合依赖它），不重复定义判定；
- 不改变通知里已有的那行「📥《任务》本地下载 x/y」文案；
- 这是纯内部改造，不动下载账本、不动 `download_items` 的对外行为。

### 4.6 界面

表单第一屏（`TaskForm.vue`），在「更新频率」上方新增一行：

```
执行方式：  ( ) 定时追更   ( ) 仅手动   ( ) 一次性
            └ 选「定时追更」时才显示：更新频率、按星期运行、截止日期
```

- 帮助文案一句：`仅手动`/`一次性` 不会被定时器触发，只能手动运行；`一次性` 跑完自动停用。
- `run_mode` 改回 `follow` 而该行仍处于停用：表单里提示"改回定时追更后记得取消停用"，**不自动改** `disabled`。
- 「停用」开关暂时留在高级区，C 项再统一收拾位置。

列表徽标（`TaskRow.vue` 的 chips）：

| 组合 | 徽标 |
| --- | --- |
| `follow` | 现有频率/星期徽标，不变 |
| `manual` | 灰「仅手动」 |
| `once` + 未停用 | 蓝「一次性待执行」 |
| `once` + 已停用 | 绿「已完成」 |
| 其它 + `disabled` | 灰「已停用」（现状不变） |

### 4.7 API 契约

- `TaskIn` / `TaskOut`（`backend/schemas.py`）加 `run_mode`，创建与更新接受该字段；非法值 400，detail 为 `执行方式只能是 follow / manual / once`。
- 外部接口 `/api/add_task`、`/api/v1/task/add`：不传时默认 `follow`（保持现有行为），传了非法值按现有约定返回 `{success: false, code: 2, message: ...}`。
- 油猴脚本与导入（`services/migrate_service.py`）不需要改：默认值即 `follow`。

## 5. 测试

后端（`.venv/bin/python -m pytest backend/tests -q`，当前基线 238 passed + 1 skipped）：

- 形态解析与非法值 400；`TaskIn/TaskOut` 往返保留 `run_mode`。
- 调度：`manual` / `once` 不注册作业；从 `follow` 改为 `manual` 时旧作业被撤销；改回 `follow` 时重新注册。
- 扫周期：`manual` / `once` 在 `trigger="scheduled"` 下被跳过，`follow` 不受影响（对照用例）。
- 收口判定六种组合（4.3 表格逐行覆盖，含"下载未执行不收口"与幂等再跑）。
- 批量运行：跳过 `disabled`；包含 `manual`/`once` 未完成；单行 `POST /{id}/run` 对 `disabled` 行仍可跑；汇总含跳过计数。
- 回归：`schedule` 非法值仍按既有行为（撤销作业 + warning + 回退全局扫）——本期只保证它不再影响非 `follow` 的任务。

前端：`npm run typecheck` + `npm run build`；真机验证覆盖三态切换后收起/展开、保存后徽标变化、`once` 跑完自动变「已完成」、改回 `follow` 时的提示文案。

## 6. 改动清单

- `backend/models.py`：`Task.run_mode`
- `backend/schemas.py`：`TaskIn` / `TaskOut` 加字段
- `backend/api/routes_tasks.py`：创建/更新校验与传值
- `backend/api/routes_external.py`：`add_task` 默认值与非法值处理
- `backend/services/task_service.py`：扫周期跳过 + 批量跳过 `disabled` + `_download_for_task` 返回结构化计数 + 一次性收口
- `backend/main.py`：`apply_task_schedule` 按形态注册/撤销
- `frontend/src/components/TaskForm.vue`：「执行方式」三选 + 条件展开 + 提示
- `frontend/src/components/TaskRow.vue`：徽标
- `frontend/src/api/types.ts`：`RunMode` 类型
- `README.md`：三态语义、与停用的分工、批量运行新语义
- `scripts/xiao-pan-auto-save.user.js`：仅在它需要传 `run_mode` 时改（本期默认值即可，预计不动）

## 7. 分阶段交付

1. 字段 + schemas + 注册/撤销 + 扫周期过滤（含单测）
2. `_download_for_task` 结构化 + 一次性收口判定 + 日志与通知文案
3. 批量「立即运行」跳过停用 + 汇总计数
4. 表单「执行方式」+ 列表徽标 + README
5. 真机验证三态与徽标显示
