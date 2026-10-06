# 一次性任务的重试预算与自动收口 — 设计文档

- 日期：2026-10-05
- 状态：待评审
- 上游：`docs/superpowers/specs/2026-10-05-task-run-modes-design.md`（本设计**修改**其 4.2 与 4.3 中「`once` 不参与任何自动驱动」这一条，其余保留）
- 范围：给 `once` 形态加"重试到拿到为止 + 三次预算 + 用尽后需手动再开"，并让 `no_changes` 与真失败走不同通道

## 1. 目标

用户提出的真实习惯：**「待更任务希望在资源真的放出时能够自动再运行一次」**，并且要求**「三次都没有成功则不再重试，需要手动再次开启」**。

拆开是两件事：

1. `once` 不再"只能手动点一次"。它应能被自动驱动，直到真的拿到资源，拿到后自动停用并从此不再驱动 —— 这正是"下载已经存在的资源，只执行一次"的实际用法：资源可能还没上架，先建好任务等着。
2. 真失败（不是"还没放出"）要有**上限**：最多三次重试，间隔 5 分钟；三次用尽就停摆，等用户手动再开，不无限骚扰网盘接口。

## 2. 非目标（明确不做）

- **不做"只列分享不转存"的轻量探测任务**。转存的检查与写入本来同源：`run_update_task` 先拉分享列表比对，没有新内容时不写网盘任何东西、只返回 `no_changes`。所以"每天看一眼"的成本与现有追更任务完全相同，再引入一层探测只会在探测与真跑之间重复列举一次分享。
- **不新增第四种形态、不新增用户可见开关**。`once` 的语义本身扩展为"重试到拿到为止"，预算与停摆是它的内在行为。
- **不做指数退避、不做可配置的重试参数**。三个固定 5 分钟，写死在常量里。
- **不新增"重新开启"端点**：行内「▶ 运行」就是这个动作（见 4.4）。
- **不改 `follow` / `manual` 的任何行为**；不改下载账本；不改 DLNA 相关（另一个会话在做）。

## 3. 关键事实（决定了设计形状）

- **调度器是内存 JobStore，进程重启作业全丢**，靠启动时 `reschedule_all_tasks()` 从数据库重建。因此"下一次几点重试"**必须是数据列**（`next_retry_at`），不能只存在作业里。
- `_auto_add_columns()`（`backend/database.py`）给非空整数列补 `NOT NULL DEFAULT 0`，给可空列补 `NULL`。所以 `retry_attempts: int = 0` 与 `next_retry_at: NaiveDatetime | None = None` 两列在存量行上分别是 `0` 和 `NULL`，**不需要额外归一化**（不同于上一个特性里 `run_mode` 撞过的 `''` 坑；仍要有测试证明升级后行为正确）。
- `once` 的"已完成"徽标是 `once + disabled` 派生（上游 spec 已接受的瑕疵）。本设计让它可区分的办法是：完成时把 `retry_attempts` 归零，而**停摆不写 `disabled`**（见 4.2），于是 `once + 未停用 + 预算耗尽` 与 `once + 停用` 天然不撞。

## 4. 设计

### 4.1 数据

`Task` 新增两列：

```python
retry_attempts: int = 0                    # 本轮已消耗的重试次数（成功或手动再开时归零）
next_retry_at: NaiveDatetime | None = None # 到点驱动的下次重试时间；None 表示没有待重试
```

常量放在 `task_service.py`：

```python
ONCE_RETRY_LIMIT = 3       # 真失败最多重试三次，用尽后停摆等手动
ONCE_RETRY_DELAY_MINUTES = 5  # 固定 5 分钟；刻意不用 1 分钟，低于表单提示的"建议 ≥5 分钟"风控线
```

### 4.2 状态机（每次驱动一个 `once` 任务后只有三种结局）

| 结局 | 判定来源 | 写库 | 徽标 |
| --- | --- | --- | --- |
| **拿到手** | 上游 `_once_verdict` 通过（有新增，且未开下载或下载实际执行且零失败） | `disabled=1`、`retry_attempts=0`、`next_retry_at=None` | 已完成 |
| **还没放出** | `result.status == "no_changes"` | **不占预算**：`retry_attempts` 不变、`next_retry_at=None`（等下一次自动扫） | 一次性待执行 |
| **真失败** | `failed` / `network` / `banned`，或 `updated` 但下载有失败 | `retry_attempts += 1`；若 `< ONCE_RETRY_LIMIT` 则 `next_retry_at = now + 5分钟`，否则 `next_retry_at = None`（停摆） | 未到上限：`重试中 N/3`；到上限：**重试已用尽** |

- 停摆**不写 `disabled`**：`disabled` 仍只表示"用户暂停"或"已完成"，避免把两种含义搅在一起。停摆的表达方式是 `retry_attempts >= ONCE_RETRY_LIMIT` 且 `next_retry_at is None`。
- `banned`（分享失效）也算真失败并计一次，因为下一次驱动仍会被 `shareurl_ban` 短路，不会形成风暴。
- 每次状态变化写一条中文日志，停摆那条必须直接告诉用户下一步动作：`三次重试仍未成功，已停止自动重试；点该行「▶ 运行」可重新开始`。

### 4.3 自动驱动规则的修订（对上游 spec 4.2 的改动）

原规则「`run_mode != "follow"` 一律不被任何自动触发驱动」修订为：

| 形态 | 任务级定时器 | 全局每日扫 | 重试 DateTrigger |
| --- | --- | --- | --- |
| `follow` | 有 `schedule` 时注册 | 无 `schedule` 时参与 | — |
| `manual` | 不注册 | 跳过 | — |
| `once` 且 `next_retry_at` 非空 | 不注册 cron/interval | 跳过（由重试作业负责，避免双驱动） | **注册一次到点作业** |
| `once` 且 `next_retry_at` 为空、预算未用尽、未停用、未过 `enddate` | 不注册 | **参与**（每天看一次，就是"等放出"） | — |
| `once` 且预算用尽 | 不注册 | 跳过（停摆） | — |

实现要求：判定收敛成一个纯函数（例如 `once_next_driver(task, now) -> str`，返回 `"retry"` / `"daily"` / `"halted"` / `"none"`），调度注册与扫周期都问它，**不在两处各写一遍条件**——上一特性已证明分散判定会漂。

> 落地注记：`now` 在计划阶段被砍掉——函数体里没人读它（过期与"到没到点"都直接用系统当前时间），留着就是死参数。实现签名是 `once_next_driver(task) -> str`。

`apply_task_schedule` 需要 `DateTrigger` 支持（`backend/core/scheduler.py` 目前只有 `interval:` / `cron:` 两种前缀）。到点作业执行后由结局判定决定要不要再排下一次，因此不需要周期作业。

### 4.4 「手动再次开启」= 点行内「▶ 运行」

`POST /api/tasks/{id}/run` 对 `once` 行的语义扩展为：**先把 `retry_attempts` 归零、清 `next_retry_at`，再立刻跑一次**，重新获得三次预算。

- 这是对既有端点行为的扩展，不是新端点、不是新按钮。前端把「重试已用尽」徽标做成提示文案「点 ▶ 运行重新开启」，让用户知道该点哪个。
- **归零是"两列 + 一个作业"，不只是两列**：`reset_once_budget()`（`POST /api/tasks/{id}/run` 进运行前调用）除了把 `retry_attempts`、`next_retry_at` 清成 `0` / `None`，还会撤掉已经排上的到点重试作业（`scheduler.unschedule_retry(task_id)`）。少了后一半，用户点完「▶ 运行」稍后会看到它又自己跑一次，等于谎报"这一按重新开启"。非 `once` 行这三样都不碰。
- 手动跑成功后仍走"拿到手"分支（自动停用）。
- 停用状态（含已完成）的行点「▶ 运行」照旧能跑（上游 4.4 已定），但**不会**偷偷把 `disabled` 改回可用 —— 那是用户自己的开关。

### 4.5 与 `enddate` 的关系

`enddate` 是既有的"过期不运行"。对 `once`：过期后按现有规则不驱动，徽标显示「已过截止」（复用 `task_due_today` 的判定，不重算日期）。不新增"过期自动停用"，避免与"已完成"共用 `disabled` 而互相伪装。

### 4.6 界面

- 徽标取值是**互斥的单一结果，按此顺序第一个命中即用**：`已过截止` → `已完成`（once+disabled）→ `重试已用尽`（未停用且预算耗尽）→ `重试中 N/3`（`next_retry_at` 有值）→ `一次性待执行`。过期排最前是因为它会让其他三个都失去意义。
- 「重试中 N/3」的 tooltip 显示 `next_retry_at` 本地时间；`重试已用尽` 的 tooltip 显示"点 ▶ 运行重新开启"。提示走徽标 `span` 上的原生 `title` 属性，**不引 `el-tooltip`**——一行文本不值得为它把 span 换成组件、也不改现有样式。
- 表单第一屏的「执行方式」提示文案改口：`一次性` 的说明从"只手动跑"改为"自动重试到拿到为止，连续三次真失败则停摆，需点运行重新开启"。

落地补记（实现与这条清单的三处口径差，都在这份 spec 的范围内，不改写上面的原文）：

- 实际判序把 `重试中 N/3`（`next_retry_at` 有值）排在 `重试已用尽` **之前**，所以「停摆」徽标的判据是 **`retry_attempts >= 3` 且 `next_retry_at` 为空**两个条件同时成立。两者只在手工改库造出的矛盾态（预算用尽 + 还挂着到点时间）上给出不同答案：后端 `once_next_driver` 对这一态判 `halted`，界面对它报「重试中」。真实写库路径不会产生这一态（4.2 的三条分支都会把 `next_retry_at` 落成 `None`），故接受这个显示口径差，前端不复制后端的判序。
- 「执行方式」的 hint 逐字是：`自动重试到拿到为止；资源没放出不算失败、每天再看一次；连续三次真失败则停摆，点「运行」重新开启`。跟着所选形态渲染在下面，**没有**再加第二条常驻说明（同一个字段两种提示是噪音）。
- 停摆行**不画灰「停用」**：`disabled` 仍是 `false`，列表上那枚灰角标本来就不会出现，用户看到的中性徽标是「重试已用尽」。

### 4.7 用户口径与中文日志（README 待 DLNA 会话提交后再补）

`README.md` 此刻带着另一会话未提交的 DLNA 改动，本次不碰它（不替别人的在制品提交），所以原本要塞进 README「执行方式」段落的那句话先落在这里，等 DLNA 会话提交后再搬过去：

> 一次性任务会自动重试到拿到资源为止：真失败最多重试 3 次、每次间隔 5 分钟，三次用尽后停摆，要点该行「▶ 运行」才重新开启（这一按同时把 `retry_attempts`/`next_retry_at` 归零并撤掉已排上的到点重试作业，不会过五分钟又自己跑一次）。分享里还没放出资源（本次无新增）**不算失败、不占预算**，改由每天一次的自动扫再来看。停摆不会把该行标成"停用"——`disabled` 仍为 `false`。

后端日志文案实际是**五条**中文（比这份 spec 计划时多一条），逐字以 `task_service.py` 为准：

| # | 文案（`《任务名》` 是前缀，`{ }` 是填充位） | 出处 |
| --- | --- | --- |
| 1 | `《x》一次性任务已完成并自动停用（{原因}）` | 拿到手 |
| 2 | `《x》本次没有新增资源（还没放出或早已转存过），不占重试预算，等下次定时检查` | 没放出 |
| 3 | `《x》本次未成功（{原因}），5 分钟后重试（{N}/3）` | 真失败、未到上限 |
| 4 | `《x》本次未成功（{原因}），已过截止日期，不再排重试（{N}/3）；点该行「▶ 运行」仍可手动跑一次` | 真失败、但行已过 `enddate` |
| 5 | `《x》三次重试仍未成功（{原因}），已停止自动重试；点该行「▶ 运行」可重新开始` | 真失败、到上限 |

第 4 条是计划阶段没单列的一支，归在 4.2 的"真失败"里：它照样 `retry_attempts += 1` 记账，但**不排重试格**（`once_next_driver` 见过期就判 `halted`，排了没人跑，留下一格就是这份设计刻意堵死的矛盾态），界面上由「已过截止」徽标说明。

## 5. 测试

`.venv/bin/python -m pytest backend/tests -q`，基线 **289 passed + 1 skipped**（含 `test_task_run_mode.py`）。必须新增：

- 状态机三种结局各自的写库断言（拿到手 / 没放出 / 真失败），其中"没放出不占预算"与"第三次失败才停摆"是核心。
- 停摆判定：第 1、2、3 次失败后 `next_retry_at` 依次被设成 now+5min、now+5min、`None`；且 `disabled` 全程保持 `False`。
- `once_next_driver` 纯函数的全分支表（含 `manual`/`follow` 返回 `"none"`、过期返回 `"halted"`、`next_retry_at` 有值时返回 `"retry"` 而**不**再被每日扫驱动）。
- 重启恢复：库里 `next_retry_at` 为未来时间 → `reschedule_all_tasks()` 后存在到点作业；为 `None` 且预算未用尽 → 参与每日扫。
- 手动再开：`POST /api/tasks/{id}/run` 先归零 `retry_attempts` 与 `next_retry_at` 再跑（断言跑完后的计数，而不是断 mock 被调）。
- 老库升级：用不含这两列的旧 `task` 表跑 `init_db()`，存量行得到 `0` / `NULL`，且原 `follow` 任务行为不变（防止重演 `run_mode` 那次误停更）。
- 幂等与旁路：收口/计数写库失败只 warn，不砍批次（沿用既有 `_settle_once` 的包裹范式）。

前端：`npm run typecheck` + `npm run build`；真机验证徽标四态与 tooltip（内嵌浏览器无渲染面，用 DOM 事件 + a11y 快照取证）。

## 6. 改动清单

- `backend/models.py`：两列
- `backend/services/task_service.py`：常量、`once_next_driver`、结局判定与写库、扫周期/注册问同一个函数
- `backend/core/scheduler.py`：新增 `reschedule_retry_at(task_id, when: datetime, func)`（`DateTrigger` 一次性作业，`replace_existing=True`）与配套 `unschedule_task` 复用。**不把重试时间塞进 `schedule` 字符串**——那个字段已经承载"频率"语义，再叠一层会重演 `run_mode` 那种"一个字段两种含义"的理解成本。
- `backend/main.py`：`apply_task_schedule` / `_run_one_task` 接新判定
- `backend/api/routes_tasks.py`：手动运行前置归零
- `backend/schemas.py`：`TaskOut` 暴露 `retry_attempts` / `next_retry_at`
- `frontend/src/api/types.ts`、`frontend/src/components/TaskRow.vue`（徽标 + tooltip）、`TaskForm.vue`（文案）
- `README.md`、`docs/superpowers/specs/2026-10-05-task-run-modes-design.md`（在 4.2/4.3 加"已被本设计修订"指向）

## 7. 分阶段

1. 两列 + `once_next_driver` 纯函数 + 全分支测试
2. 调度接入（DateTrigger 重试作业、每日扫、重启恢复、手动归零）
3. 结局判定写库 + 日志/通知文案 + 徽标与 tooltip
4. README + 上游 spec 修订注记 + 真机验证
