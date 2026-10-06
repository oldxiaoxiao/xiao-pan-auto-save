# 新建任务表单首屏与默认值 Implementation Plan

**Goal:** 把「新建任务」从 18 个字段的表单改成追剧人三填即建的首屏，修掉 `auto_download` 前后端默认值打架，让并列多目录分享默认进入逐文件过滤，合并「起始集/起始文件」两个概念，并提供只读的「试跑」预告这一跑会搬什么。

**Architecture:** 后端只加三类东西——一份 `task_defaults` 设置（不新增端点，走通用 `PUT /api/settings/{key}`）、`run_update_task(..., plan_only=True)`（**判定只有一套实现**，试跑与真实运行共用 `_check_dir`）、一个只读的 `POST /api/tasks/dry-run`。前端重排 `TaskForm.vue` 首屏、加「复制为新任务」与设置页「新建任务默认」块。`core/` 保持零数据库依赖。

**Tech Stack:** FastAPI + SQLModel/SQLite；Vue 3 `<script setup>` + Element Plus + Pinia；pytest（`asyncio_mode=auto`）+ ruff；`vue-tsc` + vite。

**Spec:** `docs/superpowers/specs/2026-10-06-task-form-first-screen-design.md`（缺陷 A-G、决定清单、破坏面、验收八条都在那里）

## Global Constraints

- 解释器一律 `.venv/bin/python`。测试 `.venv/bin/python -m pytest backend/tests -q`；lint `.venv/bin/python -m ruff check backend`；前端 `cd frontend && npm run typecheck && npm run build`。
- Python >= 3.11；ruff `line-length = 110`，`select = ["E","F","W","I","UP","B"]`，`ignore = ["E501"]`；不新增依赖。
- 基线：**321 passed + 1 skipped**（2026-10-06 实测；上一版写 317 是抄了前一特性修复波之前的数字，那波又加了 4 条）。每阶段一次中文 conventional commit。
- **判定不许出现第二套实现**：试跑必须复用 `_check_dir`（`engine.py:176-305`）的既有分支，只把三类写操作（`ensure_dir`、重存的 `delete_items`、`driver.save`/`rename`）门控掉。任何"在 dry-run 端点里重写一遍过滤/去重"都算违背本设计的核心。
- `backend/core/` 零数据库依赖（既有分层铁律）：`plan_only` 只看 `TaskSpec`；数据库与账号选择留在 services/api 层。
- 转存段仍在 `_run_lock` 内串行、下载在锁外；**`dry-run` 不占 `_run_lock`、不写库**。
- 所有 `run_mode` 读取走 `run_mode_of(task)`；新加的设置键在存量库里不存在，必须走 `DEFAULT_SETTINGS` 合并（`api/deps.py:48-53` 已如此），不许假设 `setting` 表里有行。
- 文案中文、逐字照 spec 第 4.5 节；不许出现"界面说会做而代码不做"的说法（例如把目录内过滤说成"已修复魔法重命名"——子目录内不重命名是既有行为，本轮不改）。
- `TaskIn.auto_download` 改 `True` 是**破坏面**，必须在Task 1 的提交与 README 里写明，并给外部 `add_task` 补一条回归测试。
- **绝不碰 `data/`**（真实库 + 媒体）；**绝不 `git add -A` / `git add .`**；**绝不 `git push`**。工作树里另一会话的未提交改动（`README.md` 的 DLNA 段、`docker-compose.yml`、`.dockerignore`、`deploy/`、`Dockerfile.minidlna`）**不碰不提交**；`README.md` 需要写的破坏面那段用 `git add -p` 挑自己的 hunk，若环境不支持交互分块，就把那段写进 spec 并在提交信息里注明 README 待补。
- 测试共享临时库：按自建 id 断言，不看全表计数、不依赖执行顺序；假盘驱动从 `backend.tests.test_task_service.OkDriver` / `backend.tests.test_task_run_mode.DownloadOkDriver` 继承，**不再复制一份**。

---

## 文件结构

| 文件 | 本计划职责 |
| --- | --- |
| `backend/schemas.py` | `TaskIn.auto_download` 默认 `True` |
| `backend/api/deps.py` | `DEFAULT_SETTINGS["task_defaults"]` |
| `backend/api/routes_settings.py` | `EDITABLE_KEYS` 加 `task_defaults`；`GET /api/settings/magic/expand`（只读，展开 `$TV`） |
| `backend/core/engine.py` | `run_update_task`/`_check_dir` 的 `plan_only` 与两个计数 |
| `backend/api/routes_tasks.py` | `POST /api/tasks/dry-run` |
| `frontend/src/api/types.ts`、`client.ts` | `TaskDefaults`、`DryRunResult`、`dryRun()`、`magicExpand()` |
| `frontend/src/components/TaskForm.vue` | 首屏重排、抓取范围、目录内过滤、路径跟随、停用位置、试跑 |
| `frontend/src/components/TaskRow.vue`、`views/TasksView.vue`、`stores/tasks.ts` | 「复制为新任务」 |
| `frontend/src/components/settings/SettingsTaskDefaults.vue`、`views/SettingsView.vue` | 「新建任务默认」块 |
| `backend/tests/test_task_form_defaults.py`、`backend/tests/test_dry_run.py` | 新建，本计划全部后端测试 |

---

## Task 1：后端默认值与 `task_defaults`

**Files:** Modify `backend/schemas.py`、`backend/api/deps.py`、`backend/api/routes_settings.py`；Test `backend/tests/test_task_form_defaults.py`（新建）

**Interfaces:**
- Produces：`task_defaults: dict` 设置键，形状固定为
  `{"savepath_root": str, "auto_download": bool, "run_mode": str, "pattern": str, "quality": str, "subdir_filter": bool}`
- `TaskIn.auto_download` 默认变 `True`；`TaskIn.update_subdir` 仍为 `""`（**不在后端给目录过滤设默认**，理由见 spec 4.2）
- `GET /api/settings` 的响应里出现 `task_defaults`（靠 `DEFAULT_SETTINGS` 合并，无需建行）

- [ ] **Step 1：写失败测试**

新建 `backend/tests/test_task_form_defaults.py`：

```python
"""新建任务相关默认值：TaskIn 默认、task_defaults 设置键、$TV 展开端点。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.main import app
from backend.schemas import TaskIn


def _body(**extra):
    return {"taskname": "剧集", "shareurl": "https://pan.quark.cn/s/abc", "savepath": "/剧", **extra}


def test_taskin_auto_download_default_is_true_now():
    """破坏面回归：省略该字段的外部调用方会开始下载，这条测试就是把它钉在明处。"""
    assert TaskIn(**_body()).auto_download is True
    assert TaskIn(**_body(auto_download=False)).auto_download is False  # 显式关仍然有效


def test_taskin_update_subdir_default_stays_empty():
    """目录内过滤的默认只归前端管，后端协议层保持沉默。"""
    assert TaskIn(**_body()).update_subdir == ""


def test_task_defaults_ship_with_known_shape():
    from backend.api.deps import DEFAULT_SETTINGS

    defaults = DEFAULT_SETTINGS["task_defaults"]
    assert defaults == {
        "savepath_root": "/来自：分享",
        "auto_download": True,
        "run_mode": "follow",
        "pattern": "",
        "quality": "",
        "subdir_filter": True,
    }


def test_task_defaults_roundtrip_and_not_shadowing_others():
    with TestClient(app) as c:
        assert c.get("/api/settings").json()["task_defaults"]["savepath_root"] == "/来自：分享"
        put = c.put("/api/settings/task_defaults", json={"value": {"savepath_root": "/追剧"}})
        assert put.status_code == 200
        merged = c.get("/api/settings").json()["task_defaults"]
        # 部分写入不能被当成整份替换后丢字段：缺的键仍要有默认值
        assert merged["savepath_root"] == "/追剧" and merged["subdir_filter"] is True
        assert merged["auto_download"] is True and merged["run_mode"] == "follow"
        # 别的键不受影响
        assert c.get("/api/settings").json()["crontab"]
        c.put("/api/settings/task_defaults", json={"value": dict(DEFAULT_RESTORE)})


DEFAULT_RESTORE = {
    "savepath_root": "/来自：分享",
    "auto_download": True,
    "run_mode": "follow",
    "pattern": "",
    "quality": "",
    "subdir_filter": True,
}


def test_magic_expand_returns_real_regex_without_copying_constants():
    """前端不许手抄 $TV 正则；展开只住在 MagicRename 一处。"""
    with TestClient(app) as c:
        got = c.get("/api/settings/magic/expand", params={"name": "$TV"}).json()
        assert got["ok"] is True and "mp4" in got["pattern"] and got["replace"]
        assert c.get("/api/settings/magic/expand", params={"name": "$NOPE"}).json()["ok"] is False
```

- [ ] **Step 2：跑到失败**

Run: `.venv/bin/python -m pytest backend/tests/test_task_form_defaults.py -q`
Expected: FAIL —— `KeyError: 'task_defaults'` 与 404/`ok` 字段缺失；`auto_download` 那条断言失败。

- [ ] **Step 3：实现**

`backend/schemas.py:20`：

```python
    auto_download: bool = True  # 默认下载到本地：与前端新建一致（破坏面：省略该字段的外部调用从此会下载）
```

`backend/api/deps.py` 的 `DEFAULT_SETTINGS` 里 `download` 之前加：

```python
    "task_defaults": {
        "savepath_root": "/来自：分享",
        "auto_download": True,
        "run_mode": "follow",
        "pattern": "",
        "quality": "",
        "subdir_filter": True,
    },
```

`backend/api/routes_settings.py`：`EDITABLE_KEYS` 加 `"task_defaults"`；在 `get_setting` 侧做**缺键补默认**（`set_setting` 存的是原样 JSON，部分写入不能把别的字段抹掉）。实现方式：加一个函数并在读写处各用一次——

```python
def merge_task_defaults(value: object) -> dict:
    """task_defaults 允许部分写入：缺的键回落到 DEFAULT_SETTINGS 的那一份，避免存半份就抹掉别的默认。"""
    from ..api.deps import DEFAULT_SETTINGS

    base = dict(DEFAULT_SETTINGS["task_defaults"])
    if isinstance(value, dict):
        base.update({k: v for k, v in value.items() if k in base})
    return base
```

并在 `read_settings` 返回前与 `write_one` 存前，对 `key == "task_defaults"` 各走一次它（读：`merged["task_defaults"] = merge_task_defaults(merged.get("task_defaults"))`；写：`set_setting(key, merge_task_defaults(body.value))`）。

静态路由必须排在 `GET /{key}` **之前**（否则被 `/{key}` 吃掉，`name` 会被当成设置键查不到）——本项目已有同类教训（`/downloads/history` 要排在 `/downloads/{job_id}` 之前）：

```python
@router.get("/magic/expand")
async def expand_magic(name: str) -> dict:
    """把 $TV 这类魔法关键字展开成真实正则给前端显示；展开逻辑只有 MagicRename 一处实现。"""
    from ..core.magic import MagicRename

    mr = MagicRename(get_setting("magic_regex") or {})
    pattern, replace = mr.magic_regex_conv(name, "")
    if pattern == name:  # 没命中关键字：原样返回，前端据此判定不是预设
        return {"ok": False, "name": name, "pattern": "", "replace": ""}
    return {"ok": True, "name": name, "pattern": pattern, "replace": replace}
```

- [ ] **Step 4：跑到通过 + 全量 + ruff + 提交**

```bash
.venv/bin/python -m pytest backend/tests/test_task_form_defaults.py -q
.venv/bin/python -m pytest backend/tests -q          # 317 + 新增，零失败
.venv/bin/python -m ruff check backend
git add backend/schemas.py backend/api/deps.py backend/api/routes_settings.py backend/tests/test_task_form_defaults.py
git commit -m "feat(tasks): TaskIn 默认下载到本地并新增 task_defaults 设置键与 \$TV 展开端点"
```

提交信息正文必须写一句破坏面：`省略 auto_download 的外部调用方（油猴 /api/add_task）从此会开始下载`。

**Task 1 的已知连带影响：** `backend/tests/test_external_api*` 里若有"省略 auto_download 就不下载"的断言会转红——按新默认改断言，并在测试名或注释里写清这是 4.2 的破坏面，不是回归失败。若确实没有这种断言，不要为了"顺手"去改无关测试。

---

## Task 2：引擎 `plan_only` 与只读 `dry-run`

**Files:** Modify `backend/core/engine.py`、`backend/api/routes_tasks.py`；Test `backend/tests/test_dry_run.py`（新建）

**Interfaces:**
- Consumes：`TaskSpec`（`engine.py:30-42`）、`MagicRename.is_exists/sub`、`matches_filters`（`engine.py:60`）
- Produces：
  - `run_update_task(driver, spec, magic_regex=None, log=None, plan_only: bool = False) -> TaskRunResult`
  - `TaskRunResult.planned_existing: int`、`TaskRunResult.filtered_out: int`（真实运行也照实计数，默认 0）
  - `POST /api/tasks/dry-run`，请求体 = `TaskIn`，响应 = `{ok, status, message, new_count, total_size, skipped_existing, filtered_out, items: [{name, final_name, dest_path, is_dir, size}], expanded_pattern}`
  - `_check_dir(..., plan_only: bool = False)`，递归时原样下传

- [ ] **Step 1：写失败测试（含承重的那条）**

```python
"""试跑（plan_only）与真实运行必须给出同一批结论；plan_only 不得产生任何写。"""

from __future__ import annotations

import asyncio

from backend.core.engine import TaskSpec, run_update_task
from backend.drivers.base import FsItem, SaveResult
from backend.tests.test_task_service import OkDriver

_ROOT = ["01.4K.SDR.mp4", "02.4K.SDR.mp4", "03.4K.SDR.mp4", "特典花絮.mp4", "预告片.mp4"]
_CHILD = ["11.4K.SDR.mp4", "12.4K.SDR.mp4", "花絮A.mp4"]


class PlanDriver(OkDriver):
    """两层假目录树：根里 5 个文件 + 一个「合集」目录；目标目录里**已经有**「合集」。

    这一步是刻意的：递归只发生在"目标已存在这个目录"的追更场景（engine.py:215 的 elif），
    目录不存在时它整目录搬走（engine.py:207-210），所以想让试跑真的进目录，必须先把目录摆成已存在。
    写操作全部计数，用来证明 plan_only 一次都不写。
    """

    capability = {"rename", "delete", "mkdir"}
    calls: dict[str, int] = {}

    def _bump(self, name: str) -> None:
        PlanDriver.calls[name] = PlanDriver.calls.get(name, 0) + 1

    async def list_share(self, ref, path=""):
        if not path:
            items = [FsItem(fid=f"r{i}", name=n, size=1000 + i, mtime=i) for i, n in enumerate(_ROOT)]
            items.append(FsItem(fid="d1", name="合集", is_dir=True, size=0, mtime=99))
            return items
        return [FsItem(fid=f"c{i}", name=n, size=2000 + i, mtime=i) for i, n in enumerate(_CHILD)]

    async def list_dir(self, path):
        if path.endswith("/剧"):
            return [FsItem(fid="x1", name="合集", is_dir=True)]  # 目标里已有这个目录
        return []

    async def ensure_dir(self, path):
        self._bump("ensure_dir")

    async def save(self, items, dest_path, ref=None):
        self._bump("save")
        return SaveResult(ok=True, saved=[FsItem(fid=f"s{i}", name=i.name, is_dir=i.is_dir, size=i.size)
                                          for i, _ in enumerate(items)])

    async def rename(self, fid, name):
        self._bump("rename")

    async def delete_items(self, items, purge=True):
        self._bump("delete_items")


def _spec(**kw) -> TaskSpec:
    base = dict(taskname="剧", shareurl="https://pan.quark.cn/s/x", savepath="/剧")
    base.update(kw)
    return TaskSpec(**base)


def _plan(**kw):
    PlanDriver.calls = {}
    return asyncio.run(run_update_task(PlanDriver(), _spec(**kw), plan_only=True))


def _real(**kw):
    PlanDriver.calls = {}
    return asyncio.run(run_update_task(PlanDriver(), _spec(**kw)))


def _keyset(result):
    """比较键只用 (final_name, dest_path)：**不比整个对象**，因为 plan_only 拿不到转存回执里的 new_fid。"""
    return sorted((f.final_name, f.dest_path) for f in result.files)


def test_dry_run_and_real_run_agree_on_the_same_set():
    """承重测试：同一份 spec，试跑与真实运行必须挑出完全相同的一批。"""
    planned, real = _plan(), _real()
    assert _keyset(planned) and _keyset(planned) == _keyset(real)
    assert planned.status == "updated" == real.status
    # 根目录那 5 个文件入选；「合集」因目标已存在且不递归而被跳过（不是 bug，是既有语义）
    assert {f.share_name for f in planned.files} == set(_ROOT)


def test_plan_only_writes_nothing():
    planned = _plan()
    assert PlanDriver.calls == {}, f"plan_only 居然写了：{PlanDriver.calls}"
    assert planned.files and all(f.new_fid == "" for f in planned.files)
    assert all(f.size > 0 for f in planned.files if not f.is_dir)  # total_size 的来源，别永远是 0


def test_real_run_still_writes():
    real = _real()
    assert PlanDriver.calls.get("save", 0) == 1 and _keyset(real)
    assert all(f.new_fid for f in real.files)


def test_counts_filtered_out_and_existing_files():
    planned = _plan(episode_start=1, episode_end=3, quality="4k")
    assert {f.share_name for f in planned.files} == {"01.4K.SDR.mp4", "02.4K.SDR.mp4", "03.4K.SDR.mp4"}
    assert planned.filtered_out == 2  # 特典花絮 + 预告片
    assert planned.planned_existing == 0


def test_subdir_filter_recurses_only_into_existing_dirs():
    """首屏那个开关的真实语义：update_subdir=".*" + 目标已有目录 → 进去逐文件过滤。"""
    planned = _plan(update_subdir=".*", episode_start=1, episode_end=11, quality="4k")
    names = {f.share_name for f in planned.files}
    assert "11.4K.SDR.mp4" in names and "12.4K.SDR.mp4" not in names and "花絮A.mp4" not in names
    assert any(f.dest_path.endswith("/剧/合集/11.4K.SDR.mp4") for f in planned.files)


def test_task_spec_accepts_taskin_payload():
    """dry-run 端点靠 _task_spec 直接吃 TaskIn；这条把"12 个字段同名同型"这个假设钉死。"""
    from backend.schemas import TaskIn
    from backend.services.task_service import _task_spec

    body = TaskIn(taskname="剧", shareurl="https://pan.quark.cn/s/x", savepath="/剧", pattern="$TV",
                  update_subdir=".*", episode_start=5, episode_end=9, quality="4k", ignore_extension=True)
    spec = _task_spec(body)
    assert (spec.taskname, spec.shareurl, spec.savepath, spec.pattern, spec.update_subdir) == (
        "剧", "https://pan.quark.cn/s/x", "/剧", "$TV", ".*")
    assert (spec.episode_start, spec.episode_end, spec.quality, spec.ignore_extension) == (5, 9, "4k", True)


def test_dry_run_endpoint_fails_honestly_without_network():
    """端点的两条守卫路径必须离线可测：没有支持的驱动、没有可用账号，都如实报 ok=False。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as c:
        nope = c.post("/api/tasks/dry-run", json={
            "taskname": "剧", "shareurl": "https://pan.nosuch.example/s/abc", "savepath": "/剧"}).json()
        assert nope["ok"] is False and "驱动" in nope["message"]
        noacc = c.post("/api/tasks/dry-run", json={
            "taskname": "剧", "shareurl": "https://pan.quark.cn/s/abc", "savepath": "/剧",
            "account_id": 999999}).json()
        assert noacc["ok"] is False and "账号" in noacc["message"]
```

- [ ] **Step 2：跑到失败**

Expected: FAIL —— `TypeError: run_update_task() got an unexpected keyword argument 'plan_only'`。

- [ ] **Step 3：实现 `plan_only`（只门控写，不复制判定）**

`TaskRunResult` 加两字段（`engine.py:92-95` 附近）：

```python
    planned_existing: int = 0  # 因"目标目录里已有"而跳过的条目数（目录不算，它会继续递归）
    filtered_out: int = 0  # 被集数/画质过滤拒掉的条目数
```

`run_update_task` 签名加 `plan_only: bool = False`，两处调用 `_check_dir` 时原样下传。

`_check_dir` 开头三行的写操作门控（`engine.py:192-195`）：

```python
    target_path = _norm_path(f"{spec.savepath}{rel_path}")
    if plan_only:
        # 试跑不建目录，所以列目录大概率会抛；按"列不出来=空目录=全部算新增"降级。
        # 真实运行那一侧仍走 ensure_dir + 裸 list_dir，一行都不改语义。
        dir_items = []
        try:
            dir_items = await driver.list_dir(target_path)
        except DriveError:
            pass
    else:
        await driver.ensure_dir(target_path)
        dir_items = await driver.list_dir(target_path)
    dir_names = [i.name for i in dir_items]
```

> 说明：降级分支**只在 `plan_only` 下存在**。真实运行的 `ensure_dir` + 裸 `list_dir` 一行不动，这样"改了试跑把真跑带崩"这类事在结构上不可能发生，评审也只需要盯 `plan_only` 那几条分支。

循环体内的计数与门控。**整块替换 `engine.py:197-246`**（从 `need_save: list[_Plan] = []` 到 `break`），未加注释的行与原文逐字相同——这样评审只需看带注释的行：

```python
    need_save: list[_Plan] = []
    for share_file in share_list:
        # 过滤只影响「是否入选转存」，不影响 startfid 截断：即便 startfid 文件被过滤掉，
        # 也仍需在下方 break，避免越过起始点继续转更旧的文件。
        passes = share_file.is_dir or matches_filters(
            share_file.name, spec.episode_start, spec.episode_end, spec.quality
        )
        if not passes:  # 只多这一句计数：真实运行也计，dry-run 才有「被过滤 N 项」可说
            result.filtered_out += 1
        if passes:
            search_pattern = spec.update_subdir if (share_file.is_dir and spec.update_subdir) else pattern
            if re.search(search_pattern or "", share_file.name):
                if not mr.is_exists(share_file.name, dir_names, spec.ignore_extension and not share_file.is_dir):
                    if share_file.is_dir or rel_path:
                        # 文件夹、子目录文件不重命名
                        need_save.append(_Plan(share_file, share_file.name))
                    else:
                        name_re = mr.sub(pattern, replace, share_file.name)
                        if not mr.is_exists(name_re, dir_names, spec.ignore_extension):
                            need_save.append(_Plan(share_file, name_re))
                        else:
                            result.planned_existing += 1  # 改名后又撞名：计「已存在跳过」，不改既有跳过行为
                elif share_file.is_dir and spec.update_subdir and re.search(spec.update_subdir, share_file.name):
                    if spec.update_subdir_resave and driver.has("delete"):
                        log("info", f"重存子目录：{target_path}/{share_file.name}")
                        existing = next((i for i in dir_items if i.name == share_file.name and i.is_dir), None)
                        if existing and not plan_only:  # 试跑不删：把「会重存」如实落成一条计划目录
                            await driver.delete_items([existing], purge=True)
                            dir_names.remove(existing.name)
                            dir_items.remove(existing)
                        need_save.append(_Plan(share_file, share_file.name))
                    else:
                        # 递归模式：进入分享子目录比对
                        log("info", f"检查子目录：{share_file.name}")
                        sub_share_path = f"{share_path}/{share_file.name}"
                        before = len(result.files)
                        sub_items = await driver.list_share(ref, sub_share_path)
                        if sub_items:
                            await _check_dir(
                                driver,
                                spec,
                                ref,
                                magic_regex,
                                sub_share_path,
                                f"{rel_path}/{share_file.name}",
                                sub_items,
                                log,
                                result,
                                plan_only,
                            )
                        if len(result.files) > before:
                            log("info", f"子目录有新内容：{rel_path}/{share_file.name}")
            # 目录在目标里已存在且没开递归：既有代码就是什么都不做，这里也不计 planned_existing
            # （它不是"跳过"，是"目录本身已在目标里、内容由递归或整目录搬走决定"——计了会让 UI 说谎）
        # 起始文件订阅：列表新→旧遍历，遇到 startfid（含）即停止（不受过滤影响）
        if share_file.fid == spec.startfid and spec.startfid:
            break
```

注意 `_check_dir` 的递归调用多传了一个**位置参数** `plan_only`（与函数签名末尾的 `plan_only: bool = False` 对齐），别写成关键字混用风格的一半。

**位置要在 `if not need_save: return` 之后、`items = [p.item for p in need_save]` 之前**，让 `{I}` 递增（`engine.py:248-255`）在试跑里也照样算过——否则试跑报的名字和真跑不一样。

- [ ] **Step 4：dry-run 端点**

`backend/api/routes_tasks.py` 末尾加：

```python
@router.post("/dry-run")
async def dry_run(body: TaskIn) -> dict:
    """试跑：只读地告诉你这一跑会转什么，判定与真实运行共用 engine._check_dir，不写库、不占运行锁。"""
    from ..core.engine import run_update_task
    from ..core.router import route_driver
    from ..services.task_service import _pick_account

    cls = route_driver(body.shareurl)
    if cls is None or not cls.supported:
        return {"ok": False, "status": "failed", "message": "该链接没有已支持的网盘驱动", "items": []}
    account = _pick_account(body.account_id, cls.key)
    if account is None:
        return {"ok": False, "status": "failed", "message": f"未配置可用的{cls.name}账号", "items": []}

    from ..api.deps import all_settings
    from ..config import PROXY
    from ..services.task_service import _task_spec

    spec = _task_spec(body)  # TaskIn 与 Task 字段同名，直接复用
    driver = cls(cookie=account.cookie, proxy=PROXY, index=account.sort_order)
    try:
        result = await run_update_task(driver, spec, magic_regex=all_settings().get("magic_regex") or {}, plan_only=True)
    finally:
        await driver.close()

    return {
        "ok": result.status not in ("failed", "banned", "network"),
        "status": result.status,
        "message": result.message,
        "new_count": sum(1 for f in result.files if not f.is_dir),
        "total_size": sum(f.size for f in result.files),  # SavedFile 无 size 时见下条注记
        "skipped_existing": result.planned_existing,
        "filtered_out": result.filtered_out,
        "items": [
            {"share_name": f.share_name, "final_name": f.final_name, "dest_path": f.dest_path, "is_dir": f.is_dir}
            for f in result.files[:20]
        ],
    }
```

实现时必须处理两件事，别照抄了事：
1. `SavedFile`（`engine.py:83-89`）**没有 `size` 字段**，所以 `total_size` 现在算不出来。决定：给 `SavedFile` 加 `size: int = 0`，并在**三个** append 点填 `plan.item.size` / `saved.size`（真实运行同样填，纯增字段、既有行为不变）；测试里断 `total_size > 0`，防止它永远是 0。
2. `run_update_task` 是 `async`，`driver.close()` 必须在 `finally` 里（`routes_files.py:191` 已是这个写法，照它）。

`_task_spec(body)` 直接可用：`_task_spec` 只读 `taskname/shareurl/savepath/pattern/replace/ignore_extension/startfid/update_subdir/update_subdir_resave/episode_start/episode_end/quality` 这 12 个名字（`task_service.py:105-119`），全部都在 `TaskIn` 里同名同型（已对 `schemas.py:8-30` 核过），`id`/`runweek`/`disabled` 它一个都不碰。为了让这个假设钉死，测试里加一条 `test_task_spec_accepts_taskin_payload`：用 `TaskIn(**_body())` 调 `_task_spec`，断言 12 个字段逐个等于载荷里的值。

- [ ] **Step 5：跑到通过 + 全量 + ruff + 提交**

```bash
.venv/bin/python -m pytest backend/tests/test_dry_run.py -q
.venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m ruff check backend
git add backend/core/engine.py backend/api/routes_tasks.py backend/tests/test_dry_run.py
git commit -m "feat(engine): run_update_task 加 plan_only 只读试跑，新增 dry-run 端点"
```

---

## Task 3：TaskForm 首屏重排

**Files:** Modify `frontend/src/api/types.ts`、`frontend/src/api/client.ts`、`frontend/src/components/TaskForm.vue`

**Interfaces:**
- Consumes：`task_defaults`（`GET /api/settings`）、`GET /api/settings/magic/expand`、`POST /api/tasks/dry-run`
- Produces：无新接口，只改组件；`draft.pattern`/`draft.update_subdir` 仍是唯一状态源

### Task 3 的契约代码（逐字照抄，别自创第二套）

`frontend/src/api/types.ts`：

```ts
/** GET/PUT /api/settings 的 task_defaults：只影响新建任务，绝不回写已有任务。 */
export interface TaskDefaults {
  savepath_root: string;
  auto_download: boolean;
  run_mode: RunMode;
  pattern: string;
  quality: string;
  subdir_filter: boolean;
}

/** POST /api/tasks/dry-run 的响应（只读试跑；判定与真实运行同源）。 */
export interface DryRunResult {
  ok: boolean;
  status: string;
  message: string;
  new_count?: number;
  total_size?: number;
  skipped_existing?: number;
  filtered_out?: number;
  items?: { share_name: string; final_name: string; dest_path: string; is_dir: boolean }[];
}

/** GET /api/settings/magic/expand 的响应。 */
export interface MagicExpand {
  ok: boolean;
  name: string;
  pattern: string;
  replace: string;
}
```

并在 `Settings` 里加 `task_defaults: TaskDefaults;`（`SettingKey` 是 `keyof Settings`，自动跟着覆盖，别手改那份字面量联合）。

`frontend/src/api/client.ts`（挨着 `runTask` 放，保持"任务"这一组）：

```ts
  dryRun: (body: TaskPayload) =>
    request<DryRunResult>("/api/tasks/dry-run", { method: "POST", body: JSON.stringify(body) }),
  magicExpand: (name: string) =>
    request<MagicExpand>(`/api/settings/magic/expand?name=${encodeURIComponent(name)}`),
```

`frontend/src/components/TaskForm.vue` 的脚本要点：

```ts
const DEFAULT_SUBDIR_REGEX = ".*"; // 与后端 spec 约定同值：首屏开关"开"就是它，不是别的递归正则

const defaults = computed<TaskDefaults>(() => settingsStore.settings.task_defaults);

// 新建时的初值**全部来自后端的 task_defaults**，前端不写第二份默认值。
// 为此：TaskForm 的 settings 未载入时不允许打开新建表单（见下面的 loading 规矩），
// 于是 `blank()` 永远拿到一份真值，不需要 DEFAULTS_FALLBACK 这种手抄兜底。
function blank(d: TaskDefaults): TaskPayload & { startfid_name: string } {
  return {
    taskname: "",
    shareurl: "",
    savepath: d.savepath_root, // 剧名填上后由下面的 watch 拼成 {root}/{剧名}
    pattern: d.pattern,
    replace: "",
    ignore_extension: false,
    startfid: "",
    startfid_name: "",
    update_subdir: d.subdir_filter ? DEFAULT_SUBDIR_REGEX : "",
    update_subdir_resave: false,
    enddate: "",
    runweek: [],
    auto_download: d.auto_download,
    run_mode: d.run_mode,
    download_subdir: false,
    download_savepath: "",
    disabled: false,
    account_id: null,
    sort_order: 0,
    episode_start: 0,
    episode_end: 0,
    quality: d.quality,
    schedule: "",
  };
}
```

`blank()` 的键序就是现状那份（`TaskForm.vue:35-61`）**逐键照旧，只把上面标注的 6 个取值来源换成 `d.*`**，别新增或删除键——`snapshot()` 的脏判定与 `emit("save")` 的载荷形状都吃这份键集。

调用点跟着改：`loadFrom` 里 `Object.assign(draft, blank(defaults.value), task ? {...} : {})`；`const draft = reactive(blank(defaults.value))` 在 `defaults` 尚未就绪时不能执行，所以规矩是：

```ts
// TasksView 里：设置没载入完，「＋ 新建任务」按钮 disabled 并显示"正在读取默认值…"
// 这样 TaskForm 永远只在有一份真默认值时实例化，前端不存在第二份默认。
:disabled="settingsStore.loading || !settingsStore.settings.task_defaults"
```

```ts
// 3) 抓取范围是 draft.pattern 的视图，不是第二个状态源；「自定义正则」只是展开高级区的动作项。
const captureMode = computed<"all" | "tv" | "custom">({
  get: () => (draft.pattern === "" ? "all" : draft.pattern === "$TV" ? "tv" : "custom"),
  set: (mode) => {
    if (mode === "all") draft.pattern = "";
    else if (mode === "tv") draft.pattern = "$TV";
    else openAdvancedToPattern(); // 已经是自定义值：不改 pattern，只把用户带到能改它的地方
  },
});

// 4) 目录内过滤开关：只认 "" 和 ".*" 两个值，自定义递归正则不许被开关抹掉。
const subdirFilterOn = computed({
  get: () => draft.update_subdir !== "",
  set: (on) => {
    if (on) draft.update_subdir = draft.update_subdir === "" ? DEFAULT_SUBDIR_REGEX : draft.update_subdir;
    else if (draft.update_subdir === DEFAULT_SUBDIR_REGEX) draft.update_subdir = "";
    // 关但值是别的递归正则：保持不动，让高级区那一条继续管，别静默改写用户手写的正则
  },
});

// 2) 路径跟随：只有"新建 + 未手改过路径"才跟随剧名
const pathTouched = ref(false);
watch(() => draft.taskname, (name) => {
  if (props.task || pathTouched.value) return;
  draft.savepath = name.trim() ? `${defaults.value.savepath_root}/${name.trim()}` : defaults.value.savepath_root;
});
```

`pathTouched` 的置真点**只有两处**：保存路径输入框的 `@input`、以及选择器回传路径的 `onSelectorConfirm` 里 `payload.path` 分支。程序赋值一律不置真。

`tryRun()` 的三条硬规矩：请求前 `if (!draft.shareurl.trim() || !draft.savepath.trim())` 直接 `ElMessage.warning` 不发请求；`finally` 里收 loading；结果区**原样显示** `message`，不美化成"没有新文件"之类的猜测（后端说 `banned` 就说 `banned`）。

**要落的具体改动（逐条对应 spec 4.1）**

1. **`blank()` 不再是硬编码默认**，改为接收 `task_defaults`：`auto_download` 取默认值、`update_subdir = subdir_filter ? ".*" : ""`、`pattern`/`quality`/`run_mode` 取默认，`savepath = root`（剧名填上后拼成 `{root}/{taskname}`）。
2. **`pathTouched` 标志**：只在「用户手动编辑路径输入框」或「用选择器选定路径」时置真；程序代填不置真。剧名的 `watch` 里判 `!pathTouched && !props.task` 才改路径。**编辑已有任务永不自动改路径**（`props.task` 存在即跳过）。
3. **抓取范围**三选一是 `draft.pattern` 的视图（`""` / `"$TV"` / 其它值=自定义）；「自定义正则」这一项**不新建输入框**，选中它只 `advancedOpen = ["adv"]` 并聚焦已有的 `pattern` 输入框，选完把视图值落回 `pattern` 现值。选「只抓剧集」时调 `magicExpand("$TV")` 就地显示真实正则。
4. **目录内过滤开关** = `draft.update_subdir` 的是否为 `".*"`：
   - `""` → 关；`".*"` → 开；**其它值 → 显示为开且原值不覆盖**（自定义递归正则不能被开关抹掉）
   - 关的时候只在原值是 `""` 或 `".*"` 时才清成 `""`；自定义值不动它，改为在高级区显示原值提示。
5. **「停用」按形态出现**：`props.task` 存在（编辑）时才渲染，放在「执行方式」下方同一格；新建时整块不出现，提交载荷里 `disabled: false`（后端字段照旧存在，不用改 schema）。
6. **试跑按钮**在「保存」左边：`type="default"`，文案 `试跑（只读：不转存、不下载）`，点击时把当前 draft 剥掉 `startfid_name` 后 POST `/api/tasks/dry-run`，结果渲染成一行汇总 + 前 20 条 + 失败原因；`running` 期间禁用按钮并显示"正在只读访问网盘…"。**不做自动触发**（每次都会真访问网盘）。
7. 文案逐字照 spec 4.5 表格。

**必须一起处理的既有耦合（漏了就是回归）**

- `dirty` 判定：`snapshot()` 是按键插入顺序 stringify 的（`TaskForm.vue:119-123`），而 `original` 与 `draft` 出自同一次 `loadFrom`，顺序天然一致，**所以改 `blank()` 的键序不会造成假阳性**（我上一稿说会，是夸大了，别照那句去"修"）。真正的既有事实是：`Object.assign(draft, blank(), task)` 会把服务端的 `id / retry_attempts / next_retry_at / last_run_at / shareurl_ban` 一起灌进 draft，于是它们也进了 `snapshot()` 和 PUT 请求体——今天无害（`TaskIn` 忽略多余字段）。本轮**不改这个行为**（改了要连带处理删除态与 `emit("save")` 的载荷形状，属于另一件事），但 `snapshot()` 仍建议改成排序后 stringify：一行、纯健壮性，注释写明"排序是为了让键序变化不影响脏判定"，不要顺手去剥服务端字段。
- `prefill` 通道（Task 4 的复制要用）：`loadFrom` 里 `if (!task && props.prefill) Object.assign(draft, props.prefill)` 已经在 `blank()` 之后，保持顺序即可；但要确认 prefill 里的 `update_subdir` 不被 `subdir_filter` 覆盖——规则：`prefill` 含 `update_subdir` 键时以 prefill 为准。
- `episode_start` 的现有默认：`blank()` 里是 `0`（不限）。新建时给成「0=不限」的可见文案而不是悄悄填 1（改了会误伤过滤范围）。

- [ ] **Step 1**：改 `types.ts`（`Settings` 加 `task_defaults: TaskDefaults`、`SettingKey` 自动覆盖）与 `client.ts`（`dryRun(body: TaskPayload)`、`magicExpand(name: string)`）。
- [ ] **Step 2**：TaskForm 改脚本部分（`blank`/`pathTouched`/`captureMode`/`subdirFilterOn`/`snapshot` 排序/`tryRun`），`npm run typecheck` 先过。
- [ ] **Step 3**：模板重排首屏 7 项 + 目录过滤开关 + 试跑结果区；`advancedOpen` 初始为空数组（现状保持）。
- [ ] **Step 4**：`cd frontend && npm run typecheck && npm run build`；`.venv/bin/python -m pytest backend/tests -q`（应无影响，仍 317 + Task 1-2 新增）。
- [ ] **Step 5**：提交 `git add frontend/src/api/types.ts frontend/src/api/client.ts frontend/src/components/TaskForm.vue && git commit -m "feat(ui): 新建任务首屏收敛为七项，目录内过滤与抓取范围进首屏"`。

---

## Task 4：复制为新任务 + 设置页「新建任务默认」

**Files:** Modify `frontend/src/components/TaskRow.vue`、`frontend/src/views/TasksView.vue`、`frontend/src/stores/tasks.ts`；新建 `frontend/src/components/settings/SettingsTaskDefaults.vue`；Modify `frontend/src/views/SettingsView.vue`

- [ ] **Step 1**：`TaskRow.vue` 的「⋮」下拉里加 `<el-dropdown-item command="copy">复制为新任务</el-dropdown-item>`，`emit` 类型加 `(e: "copy"): void`，`onPosition` 旁边加 `onCopy`。
- [ ] **Step 2**：`TasksView.vue` 加 `function startCopy(task: Task)`：
  - 与 `onPosition` 同款的**脏草稿守卫**（正在编辑该行且有未保存修改时直接 `ElMessage.warning` 返回），否则会拿服务端的旧值建出一条"看起来丢了修改"的复制。
  - `pendingPrefill.value = 剥掉 id / disabled / shareurl_ban / last_run_at / retry_attempts / next_retry_at / sort_order / startfid / startfid_name` 的浅拷贝（`startfid` 必剥：它是分享内 fid，跨分享留着会静默截断，spec 4.3）。
  - `pendingPrefill` 的类型若是 `Partial<TaskPayload>`，`startfid` 要显式给 `""`（不是 `undefined`，因为 `Object.assign` 会保留 `blank()` 的 `""`，这里其实是"确保为空"）。
  - 然后 `editingId.value = "new"`、`newDirty.value = false`。
- [ ] **Step 3**：同路径冲突提示——`TaskForm.vue` 里算 `duplicatePath = props.prefill?.savepath && tasks.list.some(t => t.savepath === draft.savepath && t.id !== props.task?.id)` 时在表单顶部出提示条，文案照 spec 4.3。
`frontend/src/components/settings/SettingsTaskDefaults.vue` 的骨架（照 `settings/SettingsCron.vue` 的卡片结构与保存调用方式）：

```vue
<script setup lang="ts">
import { computed } from "vue";
import { storeToRefs } from "pinia";
import { ElMessage } from "element-plus";
import { useSettingsStore } from "../../stores/settings";
import type { TaskDefaults } from "../../api/types";

const store = useSettingsStore();
const { settings } = storeToRefs(store);
const draft = computed<TaskDefaults>({
  get: () => settings.value.task_defaults,
  set: (v) => void v, // 保存走 save()，不做双向
});

async function save() {
  try {
    await store.save("task_defaults", { ...draft.value });
    ElMessage.success("已保存新建默认");
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}
</script>
```

六个控件依次是：`保存路径根`（文本）、`下载到本地`（开关）、`执行方式`（三选下拉，复用 `TaskForm` 里那份 `RUN_MODE_OPTIONS` 的 label，**不要复制第二份数组**）、`抓取范围`（全部/只抓剧集，两个值 `""` 与 `"$TV"`）、`画质`（多选，复用 `QUALITY_OPTIONS`，同样别复制）、`子目录也按集数/画质过滤`（开关）。底部固定一行 `只影响以后新建的任务，不会改动已有任务`。

> 复用 `RUN_MODE_OPTIONS` / `QUALITY_OPTIONS` 的做法：把这两个常量从 `TaskForm.vue` 提到 `frontend/src/constants.ts`（那里已经有 `MAGIC_VARIABLES`），两边各自 import 同一份。**不要在两个文件里各写一遍**——上一种特性里手抄常量已经挨过评审（`ONCE_RETRY_LIMIT_TEXT`）。

- [ ] **Step 4**：`SettingsTaskDefaults.vue`：六个控件 + 底部一句 `只影响以后新建的任务，不会改动已有任务`；保存走 `store.save("task_defaults", value)`。在 `SettingsView.vue` 的 `groups` 里插在 `cron` 之后、`magic` 之前（`{ key: "task-defaults", label: "新建默认", comp: SettingsTaskDefaults }`）。
- [ ] **Step 5**：`npm run typecheck && npm run build`；提交 `git add frontend/src/components/TaskRow.vue frontend/src/views/TasksView.vue frontend/src/components/settings/SettingsTaskDefaults.vue frontend/src/views/SettingsView.vue frontend/src/components/TaskForm.vue && git commit -m "feat(ui): 支持复制为新任务，设置页新增新建任务默认"`。

---

## Task 5：文档与真机验收

- [ ] **Step 1**：README 写破坏面（`auto_download` 默认变 `True`、新建默认 `update_subdir=".*"` 对并列多目录分享是新行为、新建表单不再有「停用」）。**只 `git add -p README.md` 挑自己的 hunk**；环境不支持交互分块时把这段留在 spec 并在提交信息里注明 README 待补。
- [ ] **Step 2**：`docs/superpowers/specs/2026-10-05-task-run-modes-design.md` 不涉及；但 `2026-10-06-task-form-first-screen-design.md` 的 4.1「目录内过滤不改变子目录内不重命名」这条要在实现后回写一行 `已实现，语义如文档所述`（如果属实；做不到就写清做不到）。
- [ ] **Step 3**：真机验收八条（spec 第 6 节）。**库副本 + 隔离实例，端口 8000 起，绝不动 8432/5173/6800**；需要真实网盘的两条（试跑只读自证、试跑数字与真实运行一致）按用户已授权的**真实账号**跑，且必须：
  - 副本库 `crontab` 设远期、所有任务 `disabled=1`、`notify_enabled/sign_enabled` 关；
  - 「只读自证」用**列目录前后对比**证明（`/api/files/dir` 两次快照差集为空）；
  - 真实运行那一条只在明确的验收任务上跑，跑完记录它转存了什么、并如实报告；
  - 结束删除副本目录（里面含真实 cookie）并 `md5` 核对 `data/xiao_pan.db`。
- [ ] **Step 4**：`.venv/bin/python -m pytest backend/tests -q` 与 `ruff` 收尾，提交 `git add docs/... README.md(可选) && git commit -m "docs: 记录新建任务默认值与试跑的破坏面与验收结果"`。

---

## 验收对照（spec → 阶段）

| Spec | 阶段 |
| --- | --- |
| 4.1 首屏 7 项、目录内过滤、停用位置 | Task 3 |
| 4.2 `auto_download=True`、`task_defaults`、后端不给 `update_subdir` 默认 | Task 1 |
| 4.3 复制为新任务（含剥 `startfid`） | Task 4 |
| 4.4 `plan_only` + `dry-run` | Task 2 |
| 4.5 文案 | Task 3、4 |
| 5 破坏面（README） | Task 5 |
| 6 测试（相等性承重测试、零写入、计数、默认回归） | Task 1、2 |
| 6 真机八条 | Task 5 |
| 2 非目标（无多套预设、不新建 vitest、不改引擎重命名规则） | 全程不得出现对应代码 |
