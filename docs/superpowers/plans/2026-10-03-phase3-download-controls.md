# 阶段3：下载任务控制（停止/暂停/继续/删除）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「下载」页从只读变为可操作:aria2 任务支持暂停/继续/停止/删除,内置任务支持停止/删除。

**Architecture:** 内置下载器加协作式取消(`asyncio.Event`,每 chunk 检查,取消即删 `.part` 置 stopped);aria2 走原生 RPC(`pause/unpause/remove/removeDownloadResult`)。每个 job 带 `source` 字段区分来源,前端据此渲染按钮。控制端点用 `?source=builtin|aria2` 查询参数分派。

**Tech Stack:** FastAPI + asyncio + httpx(后端);Vue3 + Element Plus(前端);pytest。

**Spec:** `docs/superpowers/specs/2026-10-03-simplify-schedule-control-design.md`(阶段3 章节)

## Global Constraints

- 按模式差异化:内置下载器**不支持暂停/继续**(httpx 流式无法真暂停)→ `pause`/`resume` 对 builtin 返回 400;aria2 四操作全支持。
- 向后兼容:现有 `snapshot()`/`aria2_status()` 的字段不删;只新增 `source`、新增 `stopped`/`paused` 状态取值。
- 取消是协作式的:`stop()` 置位 Event,`_fetch_one` 在 chunk 循环检查;不强行 kill 协程。
- aria2 不可达时控制端点返回 502 友好提示(复用现有 `_drive_http` 风格),不裸崩。
- 删除语义:builtin `DELETE` 仅从 registry 移除记录(不删已落盘文件);aria2 `removeDownloadResult` 清结果。真正"停止并删文件"不在本期(避免误删)。
- 用户可见文案中文。
- 测试隔离沿用 conftest 临时 DATA_DIR。

---

### Task 1: 注册表取消机制 + source 字段 + aria2 状态映射

**Files:**
- Modify: `backend/core/download_registry.py`
- Modify: `backend/services/download_service.py`(`_fetch_one` 取消检查;`aria2_status` 加 source + paused 映射)
- Test: `backend/tests/test_download_registry.py`、`backend/tests/test_download.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `registry.create(...)` 同时建 `_controls[job_id] = asyncio.Event()`;`registry.snapshot()` 每项含 `"source": "builtin"`。
  - `registry.stop(job_id) -> bool`(置位取消 Event)。
  - `registry.remove(job_id) -> bool`(从 active+done 移除记录并清理 Event)。
  - `_fetch_one(row, item, cookie_str, ua, *, job_id=None)` 在 chunk 循环检查 `registry.cancel_requested(job_id)`,置位则删 `.part`、`registry.update(job_id, status="stopped")`、返回 `(False, f"{item.name}: 已停止")`。
  - `registry.cancel_requested(job_id) -> bool`。
  - `aria2_status()` 每项含 `"source": "aria2"`,status 映射 `active→downloading / paused→paused / 其他→queued`。

- [ ] **Step 1: 写失败测试(registry 取消 + source)**

`backend/tests/test_download_registry.py` 追加:
```python
def test_snapshot_marks_builtin_source():
    from backend.core.download_registry import registry
    jid = registry.create(task_id=None, taskname="t", filename="f.mkv", dest_path="/d/f.mkv", total=10)
    row = next(j for j in registry.snapshot() if j["id"] == jid)
    assert row["source"] == "builtin"
    registry.remove(jid)


def test_stop_sets_cancel_and_remove_drops():
    from backend.core.download_registry import registry
    jid = registry.create(task_id=None, taskname="t", filename="g.mkv", dest_path="/d/g.mkv", total=10)
    assert registry.cancel_requested(jid) is False
    assert registry.stop(jid) is True
    assert registry.cancel_requested(jid) is True
    assert registry.remove(jid) is True
    assert all(j["id"] != jid for j in registry.snapshot())
    assert registry.cancel_requested(jid) is False  # 清理后无残留
```

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/pytest -q backend/tests/test_download_registry.py`
Expected: FAIL(无 stop/remove/cancel_requested/source)

- [ ] **Step 3: 实现 registry 取消 + source**

`backend/core/download_registry.py`:
- 顶部 `import asyncio`,模块级 `_controls: dict[str, asyncio.Event] = {}`。
- `create()` 末尾:`_controls[job.id] = asyncio.Event()`。
- `snapshot()`:每个 dict 加 `"source": "builtin"`(在 `asdict(job)` 基础上 `{**asdict(job), "source": "builtin"}`)。
- 新增:
```python
    def cancel_requested(self, job_id: str) -> bool:
        ev = _controls.get(job_id)
        return bool(ev and ev.is_set())

    def stop(self, job_id: str) -> bool:
        ev = _controls.get(job_id)
        if ev is None:
            return False
        ev.set()
        return True

    def remove(self, job_id: str) -> bool:
        _controls.pop(job_id, None)
        found = self._active.pop(job_id, None) is not None
        self._done = __import__("collections").deque(
            (j for j in self._done if j.id != job_id), maxlen=self._done.maxlen
        )
        return found or True
```
  并在 `update()` 里,当 status 转入非 ACTIVE(完成/失败/停止)时 `_controls.pop(job_id, None)` 清理。

- [ ] **Step 4: 实现 _fetch_one 取消检查**

`backend/services/download_service.py` `_fetch_one` 的 `async for chunk in resp.aiter_bytes(...)` 循环体开头加:
```python
                if job_id and registry.cancel_requested(job_id):
                    fh.close()
                    part.unlink(missing_ok=True)
                    registry.update(job_id, status="stopped", error="已停止")
                    return False, f"{item.name}: 已停止"
```

- [ ] **Step 5: aria2_status 加 source + paused 映射**

`aria2_status()` 里每条 out.append 的 dict 增加 `"source": "aria2"`,并把 status 计算改为:
```python
    raw = st.get("status")
    status = {"active": "downloading", "paused": "paused"}.get(raw, "queued")
```

- [ ] **Step 6: 补 _fetch_one 取消测试**

`backend/tests/test_download.py` 追加(用可控流:第一个 chunk 后请求取消):
```python
@pytest.mark.asyncio
async def test_fetch_one_cancellation_removes_part(tmp_path, monkeypatch):
    from backend.core.download_registry import registry
    class Resp:
        status_code = 200
        async def aiter_bytes(self, _n):
            yield b"x" * 10
            yield b"y" * 10
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    class Client:
        def __init__(self, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, m, u, headers=None): return Resp()
    monkeypatch.setattr(dl.httpx, "AsyncClient", Client)
    target = tmp_path / "c.mkv"
    item = dl._Item(fid="c", name="c.mkv", size=999, local_path=target)
    jid = registry.create(task_id=None, taskname="T", filename="c.mkv", dest_path=str(target), total=999)
    registry.stop(jid)  # 立即请求取消
    ok, msg = await dl._fetch_one({"download_url": "http://x", "size": 999}, item, "", "UA", job_id=jid)
    assert ok is False and "已停止" in msg
    assert not target.exists() and not list(tmp_path.glob("*.part"))
    assert next(j for j in registry.snapshot() if j["id"] == jid)["status"] == "stopped"
```

- [ ] **Step 7: 运行 + 全量 + ruff**

Run: `.venv/bin/pytest -q backend/tests/test_download_registry.py backend/tests/test_download.py` → PASS
Run: `.venv/bin/pytest -q` → 全绿;`.venv/bin/ruff check backend` → clean

- [ ] **Step 8: 提交**

```bash
git add backend/core/download_registry.py backend/services/download_service.py backend/tests/test_download_registry.py backend/tests/test_download.py
git commit -m "feat(download): 注册表协作式取消+source字段,aria2 状态映射 paused"
```

---

### Task 2: 下载控制端点（stop/pause/resume/delete + aria2 RPC）

**Files:**
- Modify: `backend/services/download_service.py`(新增 `aria2_rpc(cfg, method, *params)`)
- Modify: `backend/api/routes_downloads.py`
- Test: `backend/tests/test_download_api.py`

**Interfaces:**
- Consumes: `registry.stop/remove/cancel_requested`(T1);`DownloadSettings.from_dict(get_setting("download"))`;`_rpc_url`。
- Produces:
  - `aria2_rpc(cfg, method, *params) -> dict`(发 JSON-RPC,带 token 前缀,返回解析后的 json;异常向上抛)。
  - 端点:`POST /api/downloads/{id}/stop?source=`、`/pause?source=`、`/resume?source=`、`DELETE /api/downloads/{id}?source=`。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_download_api.py` 追加:
```python
def test_builtin_pause_resume_rejected(client_and_job):
    jid = client_and_job
    assert TestClient(app).post(f"/api/downloads/{jid}/pause?source=builtin").status_code == 400
    # stop + delete 正常
    with TestClient(app) as c:
        assert c.post(f"/api/downloads/{jid}/stop?source=builtin").json()["ok"] is True


def test_aria2_endpoints_call_rpc(monkeypatch):
    from backend.services import download_service as dl
    calls = []
    async def fake_rpc(cfg, method, *params):
        calls.append(method); return {"result": "ok"}
    monkeypatch.setattr(dl, "aria2_rpc", fake_rpc)
    with TestClient(app) as c:
        assert c.post("/api/downloads/GID123/pause?source=aria2").json()["ok"] is True
        assert c.post("/api/downloads/GID123/resume?source=aria2").json()["ok"] is True
        assert c.post("/api/downloads/GID123/stop?source=aria2").json()["ok"] is True
        assert c.delete("/api/downloads/GID123?source=aria2").json()["ok"] is True
    assert calls == ["aria2.pause", "aria2.unpause", "aria2.remove", "aria2.removeDownloadResult"]
```
（`client_and_job` fixture:建一个 builtin job 返回其 id;沿用文件已有 `client` fixture 风格,若缺则在本测试内用 `registry.create` 现造。）

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/pytest -q backend/tests/test_download_api.py` → FAIL(404/无端点)

- [ ] **Step 3: 实现 aria2_rpc**

`download_service.py` 新增:
```python
async def aria2_rpc(cfg: DownloadSettings, method: str, *params) -> dict:
    url = _rpc_url(cfg.aria2_host_port)
    token = [f"token:{cfg.aria2_secret}"] if cfg.aria2_secret else []
    payload = {"jsonrpc": "2.0", "id": "ctl", "method": method, "params": token + list(params)}
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.post(url, json=payload)
    return resp.json()
```

- [ ] **Step 4: 实现控制端点**

`routes_downloads.py` 新增(顶部 `from fastapi import HTTPException`,复用 `_dl_cfg()` 取 `DownloadSettings.from_dict(get_setting("download"))`):
```python
@router.post("/downloads/{job_id}/stop")
async def stop(job_id: str, source: str = "builtin") -> dict:
    if source == "aria2":
        return await _aria2_ctl(job_id, "aria2.remove")
    registry.stop(job_id)
    return {"ok": True}

@router.post("/downloads/{job_id}/pause")
async def pause(job_id: str, source: str = "builtin") -> dict:
    if source != "aria2":
        raise HTTPException(400, "内置下载器不支持暂停")
    return await _aria2_ctl(job_id, "aria2.pause")

@router.post("/downloads/{job_id}/resume")
async def resume(job_id: str, source: str = "builtin") -> dict:
    if source != "aria2":
        raise HTTPException(400, "内置下载器不支持继续")
    return await _aria2_ctl(job_id, "aria2.unpause")

@router.delete("/downloads/{job_id}")
async def delete(job_id: str, source: str = "builtin") -> dict:
    if source == "aria2":
        return await _aria2_ctl(job_id, "aria2.removeDownloadResult")
    registry.remove(job_id)
    return {"ok": True}

async def _aria2_ctl(gid: str, method: str) -> dict:
    from ..api.deps import get_setting
    from ..services.download_service import DownloadSettings, aria2_rpc
    cfg = DownloadSettings.from_dict(get_setting("download"))
    try:
        result = await aria2_rpc(cfg, method, gid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"aria2 不可达：{exc}") from exc
    if result.get("error"):
        raise HTTPException(502, f"aria2 报错：{result['error']}")
    return {"ok": True}
```

- [ ] **Step 5: 运行测试 + 全量 + ruff**

Run: `.venv/bin/pytest -q backend/tests/test_download_api.py` → PASS;`.venv/bin/pytest -q` 全绿;`.venv/bin/ruff check backend` clean

- [ ] **Step 6: 提交**

```bash
git add backend/services/download_service.py backend/api/routes_downloads.py backend/tests/test_download_api.py
git commit -m "feat(download): 停止/暂停/继续/删除控制端点(按 source 分派 builtin/aria2)"
```

---

### Task 3: 前端下载页操作列

**Files:**
- Modify: `frontend/src/api/types.ts`(`DownloadJob` 加 `source`;`status` 联合类型加 `stopped`/`paused`)
- Modify: `frontend/src/api/client.ts`(新增 `downloadAction(id, action, source)`、`deleteDownload(id, source)`)
- Modify: `frontend/src/views/DownloadsView.vue`(操作列 + 状态文案)
- Test: 无自动化(typecheck + build + 浏览器)

**Interfaces:**
- Consumes: T2 端点。
- Produces: 每行操作按钮,点击后 refresh。

- [ ] **Step 1: types.ts**

`DownloadJob` 增加 `source: "builtin" | "aria2"`;`status` 取值补 `"stopped" | "paused"`。

- [ ] **Step 2: client.ts**

新增:
```typescript
  downloadAction: (id: string, action: "stop" | "pause" | "resume", source: string) =>
    request<{ ok: boolean }>(`/api/downloads/${id}/${action}?source=${source}`, { method: "POST" }),
  deleteDownload: (id: string, source: string) =>
    request<{ ok: boolean }>(`/api/downloads/${id}?source=${source}`, { method: "DELETE" }),
```

- [ ] **Step 3: DownloadsView 操作列 + 文案**

- `statusText` 补 `stopped:"已停止", paused:"已暂停"`。
- 表格末尾加操作列:
```vue
      <el-table-column label="操作" width="220">
        <template #default="{ row }">
          <template v-if="row.status === 'downloading' || row.status === 'queued' || row.status === 'paused'">
            <el-button v-if="row.source === 'aria2' && row.status !== 'paused'" size="small" text @click="act(row,'pause')">暂停</el-button>
            <el-button v-if="row.source === 'aria2' && row.status === 'paused'" size="small" text @click="act(row,'resume')">继续</el-button>
            <el-button size="small" text type="warning" @click="act(row,'stop')">停止</el-button>
          </template>
          <el-button size="small" text type="danger" @click="del(row)">删除</el-button>
        </template>
      </el-table-column>
```
- script 加:
```typescript
import { ElMessage } from "element-plus";
async function act(row: DownloadJob, action: "stop" | "pause" | "resume") {
  try { await api.downloadAction(row.id, action, row.source); refresh(); }
  catch (e) { ElMessage.error((e as Error).message); }
}
async function del(row: DownloadJob) {
  try { await api.deleteDownload(row.id, row.source); refresh(); }
  catch (e) { ElMessage.error((e as Error).message); }
}
```

- [ ] **Step 4: typecheck + build**

Run: `cd frontend && npm run typecheck && npm run build` → 通过

- [ ] **Step 5: 浏览器验证**

`/downloads` 有 aria2 活跃任务时:点暂停→状态变"已暂停"、出现"继续";点继续→恢复;点停止→aria2 移除、列表消失;删除→移出。builtin 任务:无暂停/继续按钮,仅停止/删除。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts frontend/src/views/DownloadsView.vue
git commit -m "feat(ui): 下载页操作列(aria2 暂停/继续/停止/删除,builtin 停止/删除)"
```

---

## Self-Review

- **Spec 覆盖**:阶段3 全部要点 → T1(source/paused 映射、registry 取消、_fetch_one stopped)、T2(四端点 + aria2_rpc + builtin 拒绝暂停)、T3(前端操作列 + 文案)。
- **占位符**:无 TBD;各步含完整代码。`client_and_job` fixture 若缺,测试内现造(已注明)。
- **类型一致**:`registry.stop/remove/cancel_requested` 签名在 T1 定义、T2 调用一致;端点 `?source=` 与前端 `row.source` 一致;`DownloadJob.source` 贯穿 types/client/view。
- **依赖顺序**:T1 → T2(依赖 T1 的 registry 方法)→ T3(依赖 T2 端点)。
