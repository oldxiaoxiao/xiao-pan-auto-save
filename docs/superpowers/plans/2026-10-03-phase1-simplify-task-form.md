# 阶段1：任务表单简化 + 集数/画质字段 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让"下载某剧第 X 到 Y 集、指定画质"变成填基础字段即可,无需手写正则;任务表单默认只暴露高频字段,个性化字段折叠进高级区。

**Architecture:** 后端给 `Task` 加 `episode_start/episode_end/quality` 三个带默认值的列(启动时 `_auto_add_columns` 自动迁移),引擎在选中叶子文件后叠加一层后置过滤 `matches_filters`(目录恒放行),集数用新 helper `extract_episode` 从文件名提取。前端 `TaskForm.vue` 拆成基础区 + 默认收起的高级区。

**Tech Stack:** FastAPI + SQLModel + SQLite(后端);Vue3 + TS + Element Plus(前端);pytest + pytest-asyncio。

**Spec:** `docs/superpowers/specs/2026-10-03-simplify-schedule-control-design.md`(阶段1 章节)

## Global Constraints

- 向后兼容:新增字段一律带默认值(`episode_start=0`/`episode_end=0`/`quality=""`);三者为默认时,引擎过滤行为与改造前完全一致。
- 不替换现有 `pattern`/`replace`/`$TV` 正则:集数/画质是叠加其后置过滤。
- `matches_filters` 仅对**非目录叶子文件**生效;目录恒 `True`(否则误杀整棵子树)。
- 画质 token 匹配带词边界(避免 `4k` 命中 `14k`),不区分大小写,多选 OR。
- 测试隔离沿用 `backend/tests/conftest.py` 临时 `DATA_DIR`;pytest 不得触碰真实库。
- 前端"默认大于配置":基础区只显高频字段,高级区默认收起但不删任何字段。
- 用户可见文案一律中文。
- 迁移依赖 `database.py:_auto_add_columns()`,不写迁移脚本。

---

### Task 1: 集数提取 helper `extract_episode`

**Files:**
- Modify: `backend/core/magic.py`(在 `PRIORITY_LIST` 定义后新增模块级函数)
- Test: `backend/tests/test_magic.py`

**Interfaces:**
- Consumes: `DEFAULT_MAGIC_VARIABLES["{E}"]`(已存在,候选正则列表)
- Produces: `extract_episode(name: str) -> int | None` — 按候选顺序取首个数字并转 int;无匹配返回 `None`。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_magic.py` 末尾追加:

```python
from backend.core.magic import extract_episode


def test_extract_episode_variants():
    assert extract_episode("第05集.mp4") == 5
    assert extract_episode("凡人修仙传.E193.mkv") == 193
    assert extract_episode("S02E15.mp4") == 15
    assert extract_episode("01.mp4") == 1
    assert extract_episode("预告片 1080p.mp4") in (1080, None)  # 宽松候选可能取到 1080，允许 None
    assert extract_episode("无数字标题.mkv") is None
```

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/pytest backend/tests/test_magic.py::test_extract_episode_variants -v`
Expected: FAIL — `ImportError: cannot import name 'extract_episode'`

- [ ] **Step 3: 实现**

在 `backend/core/magic.py` 的 `PRIORITY_LIST = ...` 行之后新增:

```python
def extract_episode(name: str) -> int | None:
    """按 {E} 候选正则顺序从文件名取首个集数整数；无匹配返回 None。

    候选较宽松（含 (?<!\\d)\\d{1,3}(?!\\d) 等），可能把年份/体积误判为集数，
    与现有重命名同源，追更场景可接受。
    """
    for pat in DEFAULT_MAGIC_VARIABLES["{E}"]:
        if m := re.search(pat, name):
            digits = "".join(c for c in m.group() if c.isdigit())
            if digits:
                return int(digits)
    return None
```

- [ ] **Step 4: 运行验证通过**

Run: `.venv/bin/pytest backend/tests/test_magic.py::test_extract_episode_variants -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/core/magic.py backend/tests/test_magic.py
git commit -m "feat(engine): 新增 extract_episode 从文件名提取集数"
```

---

### Task 2: Task 模型/Schema/TaskSpec 三字段贯通

**Files:**
- Modify: `backend/models.py:57`（Task 内 `sort_order` 附近）
- Modify: `backend/schemas.py:8-25`（`TaskIn`）
- Modify: `backend/core/engine.py:29-39`（`TaskSpec`）
- Modify: `backend/services/task_service.py:35-46`（`_task_spec`）
- Modify: `frontend/src/api/types.ts`（`TaskPayload`）
- Test: `backend/tests/test_api.py`

**Interfaces:**
- Consumes: 无
- Produces: `Task.episode_start:int`、`Task.episode_end:int`、`Task.quality:str`;`TaskSpec` 同名三字段;`TaskIn` 三字段;前端 `TaskPayload` 三字段。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_api.py` 末尾追加:

```python
def test_task_episode_quality_fields(client):
    body = {
        "taskname": "集数画质任务",
        "shareurl": "https://pan.quark.cn/s/xyz",
        "savepath": "/动漫/剧",
        "episode_start": 1,
        "episode_end": 20,
        "quality": "1080p,4k",
    }
    created = client.post("/api/tasks", json=body).json()
    assert created["episode_start"] == 1 and created["episode_end"] == 20
    assert created["quality"] == "1080p,4k"

    # 默认值向后兼容：不传即 0/0/""
    plain = client.post(
        "/api/tasks",
        json={"taskname": "默认", "shareurl": "https://pan.quark.cn/s/d", "savepath": "/d"},
    ).json()
    assert plain["episode_start"] == 0 and plain["episode_end"] == 0 and plain["quality"] == ""
    client.delete(f"/api/tasks/{created['id']}")
    client.delete(f"/api/tasks/{plain['id']}")
```

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/pytest backend/tests/test_api.py::test_task_episode_quality_fields -v`
Expected: FAIL — 返回体无 `episode_start` 字段(KeyError 或断言失败)

- [ ] **Step 3: 加模型字段**

`backend/models.py` 的 `Task` 类内,`sort_order: int = 0` 行下方加:

```python
    episode_start: int = 0  # 起始集（含），0=不限
    episode_end: int = 0  # 结束集（含），0=不限
    quality: str = ""  # 逗号分隔 token，如 "1080p,4k"，空=不限
```

- [ ] **Step 4: 加 Schema 字段**

`backend/schemas.py` 的 `TaskIn` 内,`sort_order: int = 0` 行下方加:

```python
    episode_start: int = 0
    episode_end: int = 0
    quality: str = ""
```

- [ ] **Step 5: 加 TaskSpec 字段**

`backend/core/engine.py` 的 `TaskSpec` dataclass 内,`update_subdir_resave: bool = False` 下方加:

```python
    episode_start: int = 0
    episode_end: int = 0
    quality: str = ""
```

- [ ] **Step 6: _task_spec 填充**

`backend/services/task_service.py` 的 `_task_spec` 内,`update_subdir_resave=task.update_subdir_resave,` 下方加:

```python
        episode_start=task.episode_start,
        episode_end=task.episode_end,
        quality=task.quality,
```

- [ ] **Step 7: 前端类型**

`frontend/src/api/types.ts` 的 `TaskPayload` 内,`sort_order: number;` 下方加:

```typescript
  episode_start: number;
  episode_end: number;
  quality: string;
```

- [ ] **Step 8: 运行后端测试通过**

Run: `.venv/bin/pytest backend/tests/test_api.py::test_task_episode_quality_fields -v`
Expected: PASS

- [ ] **Step 9: 前端类型检查**

Run: `cd frontend && npm run typecheck`
Expected: 通过(若 `TaskForm.vue` 的 `blank()` 缺新字段会报错,Task 4 补齐;此步允许因 TaskForm 未改而报缺字段,则记录并留待 Task 4)

- [ ] **Step 10: 提交**

```bash
git add backend/models.py backend/schemas.py backend/core/engine.py backend/services/task_service.py frontend/src/api/types.ts backend/tests/test_api.py
git commit -m "feat(task): 新增 episode_start/episode_end/quality 三字段贯通模型/Schema/TaskSpec/前端类型"
```

---

### Task 3: 后置过滤 `matches_filters` 接入引擎

**Files:**
- Modify: `backend/core/engine.py`(新增 `matches_filters`;`_check_dir` 循环 line ~160 接入)
- Test: `backend/tests/test_engine.py`

**Interfaces:**
- Consumes: `extract_episode`(Task 1);`TaskSpec.episode_start/episode_end/quality`(Task 2)
- Produces: `matches_filters(name: str, ep_start: int, ep_end: int, quality: str) -> bool`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_engine.py` 末尾追加:

```python
from backend.core.engine import matches_filters


def test_matches_filters_quality_or():
    assert matches_filters("剧.S02.1080p.mkv", 0, 0, "1080p,4k") is True
    assert matches_filters("剧.4K.mkv", 0, 0, "1080p,4k") is True
    assert matches_filters("剧.720p.mkv", 0, 0, "1080p,4k") is False
    assert matches_filters("剧.mkv", 0, 0, "") is True  # 不限画质
    assert matches_filters("14k.mkv", 0, 0, "4k") is False  # 词边界，不误命中


def test_matches_filters_episode_range():
    assert matches_filters("第05集.mp4", 1, 20, "") is True
    assert matches_filters("第25集.mp4", 1, 20, "") is False
    assert matches_filters("第01集.mp4", 1, 1, "") is True  # 边界含
    assert matches_filters("第01集.mp4", 0, 0, "") is True  # 未设区间放行
    assert matches_filters("花絮无集数.mp4", 1, 20, "") is False  # 设了区间但提不出集数


def test_matches_filters_empty_passes_all():
    assert matches_filters("任意文件名", 0, 0, "") is True


@pytest.mark.asyncio
async def test_engine_applies_quality_filter_on_files():
    drv = FakeDriver(
        {"": [f("1", "ep1.1080p.mp4"), f("2", "ep2.4k.mp4"), f("3", "ep3.720p.mp4")]},
    )
    res = await run_update_task(drv, spec(quality="1080p,4k"))
    assert sorted(i.share_name for i in res.files) == ["ep1.1080p.mp4", "ep2.4k.mp4"]


@pytest.mark.asyncio
async def test_engine_episode_filter_skips_dirs():
    # 目录名无集数，设了区间也不能误杀整棵子树
    drv = FakeDriver(
        {
            "": [d("10", "4K"), f("11", "readme.mp4")],
            "/4K": [f("1", "第03集.mp4"), f("2", "第30集.mp4")],
        },
        dirs={"/动漫/测试剧": []},
    )
    res = await run_update_task(drv, spec(update_subdir="4K", episode_start=1, episode_end=10))
    assert [i.share_name for i in res.files] == ["第03集.mp4"]
```

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/pytest backend/tests/test_engine.py::test_matches_filters_quality_or -v`
Expected: FAIL — `cannot import name 'matches_filters'`

- [ ] **Step 3: 实现 matches_filters**

`backend/core/engine.py` 顶部确保已 `import re` 且 `from .magic import MagicRename, extract_episode`。在 `TaskSpec` 定义之后新增:

```python
def matches_filters(name: str, ep_start: int, ep_end: int, quality: str) -> bool:
    """叶子文件后置过滤：画质 token（词边界、OR）+ 集数区间。

    仅对非目录文件调用（目录由调用方恒放行）。ep 区间与 quality 全默认时恒 True。
    """
    if quality:
        low = name.lower()
        toks = [t.strip().lower() for t in quality.split(",") if t.strip()]
        if toks and not any(re.search(rf"(?<![0-9a-z]){re.escape(t)}(?![0-9a-z])", low) for t in toks):
            return False
    if ep_start or ep_end:
        ep = extract_episode(name)
        if ep is None:
            return False
        if ep_start and ep < ep_start:
            return False
        if ep_end and ep > ep_end:
            return False
    return True
```

- [ ] **Step 4: 接入 _check_dir 循环**

`backend/core/engine.py` 的 `_check_dir` 内,`for share_file in share_list:` 循环体第一行(`search_pattern = ...` 之前)插入目录放行的过滤:

```python
        if not share_file.is_dir and not matches_filters(
            share_file.name, spec.episode_start, spec.episode_end, spec.quality
        ):
            continue
```

- [ ] **Step 5: 运行本任务测试通过**

Run: `.venv/bin/pytest backend/tests/test_engine.py -k "matches_filters or quality_filter or episode_filter" -v`
Expected: 全部 PASS

- [ ] **Step 6: 全量回归**

Run: `.venv/bin/pytest -q`
Expected: 全绿(默认字段=旧行为,不破坏既有引擎用例)

- [ ] **Step 7: ruff**

Run: `.venv/bin/ruff check backend/core/engine.py`
Expected: All checks passed

- [ ] **Step 8: 提交**

```bash
git add backend/core/engine.py backend/tests/test_engine.py
git commit -m "feat(engine): 集数/画质后置过滤接入转存引擎（目录放行）"
```

---

### Task 4: 前端表单两层布局

**Files:**
- Modify: `frontend/src/components/TaskForm.vue`
- Test: 无自动化(以 `npm run typecheck` + 手动/构建校验)

**Interfaces:**
- Consumes: `TaskPayload` 三字段(Task 2)
- Produces: 基础区含集数/画质;高级区含其余个性化字段

- [ ] **Step 1: blank() 补默认值**

`TaskForm.vue` 的 `blank()` 返回对象内,`sort_order: 0,` 下方加:

```typescript
    episode_start: 0,
    episode_end: 0,
    quality: "",
```

- [ ] **Step 2: 画质预设常量**

`TaskForm.vue` `<script setup>` 顶部 `import` 之后加:

```typescript
const QUALITY_OPTIONS = ["4K", "1080P", "720P", "x265", "HDR"];
// draft.quality 以逗号分隔存储；UI 用数组双向映射
const qualityList = computed({
  get: () => (draft.quality ? draft.quality.split(",").filter(Boolean) : []),
  set: (v: string[]) => (draft.quality = v.join(",")),
});
```

- [ ] **Step 3: 基础集数/画质控件**

在模板"保存路径"字段块(`</div>` 收尾 `f--wide` 保存路径)之后、"匹配正则 pattern"字段块之前,插入基础区新字段:

```vue
      <div class="f">
        <label class="field-label">起始集（含，0=不限）</label>
        <el-input-number v-model="draft.episode_start" :min="0" :max="9999" controls-position="right" />
      </div>
      <div class="f">
        <label class="field-label">结束集（含，0=不限）</label>
        <el-input-number v-model="draft.episode_end" :min="0" :max="9999" controls-position="right" />
      </div>
      <div class="f f--wide">
        <label class="field-label">画质（多选，留空=不限）</label>
        <el-select v-model="qualityList" multiple clearable placeholder="不限画质" style="width: 100%">
          <el-option v-for="q in QUALITY_OPTIONS" :key="q" :label="q" :value="q" />
        </el-select>
      </div>
```

- [ ] **Step 4: 其余字段折叠进高级区**

把模板 `<div class="grid">` 内、从"匹配正则 pattern"起到"停用"开关止的**全部现有字段块**,整体包进一个默认收起的高级区(集数/画质/名称/链接/路径/下载到本地 留在外层基础区)。用 Element Plus 折叠:

```vue
    <el-collapse v-model="advancedOpen" class="adv">
      <el-collapse-item title="高级设置（正则/魔法变量/子目录/截止日期等）" name="adv">
        <div class="grid">
          <!-- 原 pattern/replace/魔法变量/ignore_extension/startfid/
               update_subdir/update_subdir_resave/download_subdir/
               download_savepath/enddate/指定账号/runweek/disabled 字段块移入此处 -->
        </div>
      </el-collapse-item>
    </el-collapse>
```

`<script setup>` 内加:

```typescript
const advancedOpen = ref<string[]>([]); // 默认收起
```

注:`下载到本地(auto_download)` 开关保留在基础区(高频);"递归下载子目录/本地下载子目录"移入高级区。

- [ ] **Step 5: 类型检查 + 构建**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: 通过,无 TS 报错

- [ ] **Step 6: 手动验证**

启动 `npm run dev` + 后端,打开"新建任务":基础区只见 名称/链接/路径/起始集/结束集/画质/下载到本地;高级区默认收起、展开可见全部原字段;编辑已有任务能正确回填集数/画质;保存后 `GET /api/tasks` 返回体含正确三字段。

- [ ] **Step 7: 提交**

```bash
cd /Users/chengguixiao/superFolder/project/xiao-pan-auto-save
git add frontend/src/components/TaskForm.vue
git commit -m "feat(ui): 任务表单两层化——集数/画质进基础区，个性化字段折叠高级区"
```

---

## Self-Review

**Spec 覆盖:** 阶段1 全部要求 → Task1(extract_episode)、Task2(三字段贯通+向后兼容)、Task3(matches_filters+目录放行+词边界+区间+集成测试)、Task4(表单两层)。均有对应任务。

**占位符扫描:** 无 TBD/TODO;所有代码步骤含完整代码。Task4 Step4 的注释是"移入此处"的迁移指引(明确源字段清单),非占位。

**类型一致性:** `extract_episode(name)->int|None`(T1)被 `matches_filters`(T3)调用一致;`matches_filters(name,ep_start,ep_end,quality)` 签名在 T3 测试与实现一致;三字段名 `episode_start/episode_end/quality` 在 models/schemas/engine/task_service/types/TaskForm 全程一致。

**依赖顺序:** T1 → T2 → T3(依赖 T1+T2)→ T4(依赖 T2)。串行执行。
