"""aria2 真机端到端活体验证（opt-in）：投递 → 落盘 → 对账（RPC/文件兜底）→ 重下 → Emby 接线。

默认跳过，普通套件永不依赖 Docker。启用方式（需真 aria2 环境就绪，见下）：
    XIAO_PAN_ARIA2_E2E=1 .venv/bin/python -m pytest backend/tests/test_download_aria2_live.py -q

环境契约（由外部准备，本测试不创建/不停止任何容器）：
- aria2 1.36.0 守护进程：127.0.0.1:6801/jsonrpc，secret P3TERX，容器 xiao-pan-aria2-e2e；
- 其下载目录在宿主机与容器内 bind-mount 于同一绝对路径 /tmp/xiao-pan-aria2-e2e/downloads，
  账本里的 dest_path 可直接在宿主机校验；
- 容器（Docker Linux VM）经 http://host.docker.internal:<port> 回连宿主机上的测试 stub 服务，
  测试用 stub 提供真实直链字节并记录收到的请求；
- 全部临时文件在 /tmp/xiao-pan-aria2-e2e/ 下；账本走 conftest 的临时 DATA_DIR 库，
  绝不接触仓库 data/；每个用例自行清理 daemon 侧 download result 与自己的落盘目录。

断言只看真实结果：宿主机上的文件字节/md5、库里读回的账本行状态迁移、stub 收到的请求行，
不数 mock 调用次数。"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from sqlmodel import col, select

from backend.database import session_scope
from backend.models import DownloadRecord
from backend.services import download_history as hist
from backend.services import download_service as dl
from backend.services.download_service import DownloadSettings
from backend.tests.test_download import DlDriver, saved

RPC_URL = "http://127.0.0.1:6801/jsonrpc"
SECRET = "P3TERX"
TOKEN = f"token:{SECRET}"
SCRATCH = Path("/tmp/xiao-pan-aria2-e2e")
DOWNLOADS = SCRATCH / "downloads"
SERVE_DIR = SCRATCH / "serve"
GID_RE = re.compile(r"[0-9a-f]{16}")  # 真 aria2 的 gid 是 16 位十六进制


def _daemon_version() -> str:
    try:
        resp = httpx.post(RPC_URL, json={"jsonrpc": "2.0", "id": "probe", "method": "aria2.getVersion",
                                         "params": [TOKEN]}, timeout=3)
        return str(resp.json().get("result", {}).get("version", ""))
    except Exception:  # noqa: BLE001
        return ""


if not os.environ.get("XIAO_PAN_ARIA2_E2E"):
    pytest.skip("opt-in：需 XIAO_PAN_ARIA2_E2E=1 且真 aria2 容器就绪（运行方式见文件头）", allow_module_level=True)
if not _daemon_version():
    pytest.skip(f"aria2 守护进程不可达：{RPC_URL}（getVersion 探测失败）", allow_module_level=True)


def ev(label: str, obj) -> None:
    """活体证据行（pytest -s 下进 stdout，报告逐条引用）。"""
    print(f"\n[E2E] {label}: {obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)}")


def rpc(method: str, *params) -> dict:
    """测试脚手架自己的同步 RPC（getVersion/addUri 之外的清理、观测用）。"""
    resp = httpx.post(RPC_URL, json={"jsonrpc": "2.0", "id": "e2e", "method": method,
                                     "params": [TOKEN, *params]}, timeout=8)
    return resp.json()


def wait_until(fn, why: str, timeout: float = 90.0, interval: float = 0.4):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    ev(f"超时前最后观测（{why}）", last)
    raise AssertionError(f"aria2 活体等待超时（{timeout}s）：{why}")


def wait_result(gid: str, want: str = "complete") -> dict:
    """轮询真机结果直到 want；提前出现 error 就把 raw struct 原样打进证据并失败。

    注意：真机 system.listMethods 里没有 aria2.tellDownloadResult 这个方法（那是我们 fake 测试
    生造的），查已完成/失败作业的结果结构只能用 aria2.tellStatus。"""
    keys = ["status", "completedLength", "totalLength", "errorCode", "errorMessage"]

    def once():
        struct = rpc("aria2.tellStatus", gid, keys).get("result") or {}
        status = struct.get("status")
        if status == want:
            return struct
        if status == "error":
            raise AssertionError(f"aria2 真机报错（gid {gid}）raw struct: {json.dumps(struct, ensure_ascii=False)}")
        return None
    return wait_until(once, why=f"gid {gid} 结果变为 {want}")


# ---------------------------------------------------------------- stub 服务（真实直链源 + Emby 假身）

class _HubHandler(SimpleHTTPRequestHandler):
    """GET 按真实字节响应（aria2 抓它 = 真下载）；POST 一律 204 只记账（Emby 假身）。"""

    records: list[dict] = []
    root: str = str(SCRATCH)

    def __init__(self, *args, **kw):
        super().__init__(*args, directory=self.root, **kw)

    def log_message(self, *args):  # 静音
        pass

    def _record(self):
        _HubHandler.records.append({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })

    def do_GET(self):
        self._record()
        super().do_GET()

    def do_POST(self):
        self._record()
        self.send_response(204)
        self.end_headers()


class Hub:
    def __init__(self, port: int, records: list[dict]):
        self.port = port
        self.records = records

    @property
    def docker_base(self) -> str:
        """容器内回连宿主机的基址（aria2 用的直链域名）。"""
        return f"http://host.docker.internal:{self.port}"

    @property
    def local_base(self) -> str:
        """宿主机侧基址（Emby 刷新调用发生在应用进程内，走 127.0.0.1 即可）。"""
        return f"http://127.0.0.1:{self.port}"

    def gets_for(self, name: str) -> list[dict]:
        return [r for r in self.records if r["method"] == "GET" and name in r["path"]]


@pytest.fixture(scope="module")
def hub():
    SERVE_DIR.mkdir(parents=True, exist_ok=True)
    _HubHandler.root = str(SERVE_DIR)
    _HubHandler.records = records = []
    srv = ThreadingHTTPServer(("0.0.0.0", 0), _HubHandler)  # 0 号端口让 OS 选空闲口，避开 8432/8433
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield Hub(srv.server_address[1], records)
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        shutil.rmtree(SERVE_DIR, ignore_errors=True)


def source_file(tag: str, size: int = 256 * 1024) -> dict:
    """在 stub 服务目录下放一个确定性字节文件，返回驱动行需要的 name/size 加校验用 md5。"""
    name = f"e2e-source-{tag}.bin"
    payload = random.Random(f"xiao-pan-aria2-{tag}").randbytes(size)
    (SERVE_DIR / name).write_bytes(payload)
    return {"name": name, "size": size, "md5": hashlib.md5(payload).hexdigest()}


# ---------------------------------------------------------------- 驱动与配置

class LiveDlDriver(DlDriver):
    """复用仓库测试假驱动的骨架，只把直链换成 host.docker.internal 真 URL + 真实 size。"""

    default_files: dict[str, dict] = {}
    default_base = ""

    def __init__(self, children=None, *, files: dict[str, dict] | None = None, base: str = "", **kw):
        super().__init__(children=children, **kw)
        self.files = dict(files) if files is not None else dict(self.default_files)
        self.base = base or self.default_base

    async def get_download_urls(self, fids):
        self.requested.append(list(fids))
        rows = [
            {"fid": f, "file_name": self.files[f]["name"], "size": self.files[f]["size"],
             "download_url": f"{self.base}/{self.files[f]['name']}"}
            for f in fids if f in self.files
        ]
        return rows, "E2E-CK=1"


def live_cfg(tag: str, **kw) -> DownloadSettings:
    base = dict(mode="aria2", dir=str(DOWNLOADS / f"e2e-{tag}"),
                aria2_host_port="127.0.0.1:6801", aria2_secret=SECRET)
    base.update(kw)
    return DownloadSettings(**base)


class _Acc:  # retry_record 的账号替身（语义同 test_download_history._Acc）
    id, cookie, sort_order, driver_key = 91, "ck", 3, "fake"


# ---------------------------------------------------------------- 账本脚手架

def snapshot(task_id: int) -> list[dict]:
    with session_scope() as s:
        rows = s.exec(
            select(DownloadRecord).where(col(DownloadRecord.task_id) == task_id).order_by(col(DownloadRecord.id))
        ).all()
        return [r.model_dump() for r in rows]


def ref_row(ref: str) -> dict:
    with session_scope() as s:
        return s.exec(
            select(DownloadRecord).where(col(DownloadRecord.ref_id) == ref).order_by(col(DownloadRecord.id).desc())
        ).first().model_dump()


def _reset_to_queued(ref: str) -> None:
    """把行复位成非终态。这是本用例 scratch 库里的直接 UPDATE（测试脚手架，不是生产路径）。"""
    with session_scope() as s:
        row = s.exec(select(DownloadRecord).where(col(DownloadRecord.ref_id) == ref)
                     .order_by(col(DownloadRecord.id).desc())).first()
        row.status = "queued"
        row.finished_at = None
        s.add(row)


@pytest.fixture
def e2e():
    """用例级清场：daemon 侧 removeDownloadResult、scratch 目录、遗留非终态行（直接 UPDATE 收口）。"""
    ctx = {"gids": [], "dirs": [], "refs": []}
    yield ctx
    for gid in ctx["gids"]:
        try:
            rpc("aria2.removeDownloadResult", gid)
        except Exception:  # noqa: BLE001
            pass
    for ref, source in ctx["refs"]:
        with session_scope() as s:
            row = s.exec(select(DownloadRecord).where(col(DownloadRecord.ref_id) == ref,
                         col(DownloadRecord.source) == source).order_by(col(DownloadRecord.id).desc())).first()
            if row is not None and row.status not in hist.TERMINAL:
                row.status = "stopped"
                row.finished_at = datetime.now()
                s.add(row)
    for d in ctx["dirs"]:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 六项活体验证

async def test_01_submit_writes_queued_row_and_file_lands(hub, e2e):
    """第 1+2 项：download_items aria2 模式投递真 daemon → 账本 queued 行（真 gid）→ 真机把文件落进挂载目录。"""
    src = source_file("t1")
    driver = LiveDlDriver(files={"FID-T1": src}, base=hub.docker_base)
    c = live_cfg("t1")
    e2e["dirs"].append(c.dir)
    lines = await dl.download_task_files(
        driver, [saved("FID-T1", "第1集.mkv", "/电视剧/第1集.mkv")], c,
        task_id=9601, taskname="E2E活体", account_id=7,
    )
    ev("投递返回行", lines)
    assert lines and lines[0].startswith("✅")

    rows = snapshot(9601)
    assert len(rows) == 1
    r = rows[0]
    gid = r["ref_id"]
    e2e["gids"].append(gid)
    e2e["refs"].append((gid, "aria2"))
    assert r["status"] == "queued" and r["source"] == "aria2" and r["fid"] == "FID-T1"
    assert GID_RE.fullmatch(gid), f"真机 gid 应为 16 位十六进制: {gid!r}"
    assert r["dest_path"] == str(Path(c.dir) / "电视剧" / "第1集.mkv")
    assert r["size_total"] == src["size"] and r["account_id"] == 7 and r["taskname"] == "E2E活体"
    ev("投递后账本行", r)

    struct = wait_result(gid, "complete")
    ev("tellDownloadResult(complete)", struct)

    dest = Path(r["dest_path"])
    assert dest.exists() and dest.stat().st_size == src["size"]
    md5 = hashlib.md5(dest.read_bytes()).hexdigest()
    ev("落盘校验", {"dest": str(dest), "size": src["size"], "md5_source": src["md5"], "md5_on_host": md5})
    assert md5 == src["md5"]

    gets = hub.gets_for(src["name"])
    assert gets, "stub 服务未收到容器侧 GET（host.docker.internal 直链没被真实请求）"
    head = gets[-1]
    ev("源站收到的 GET", {"path": head["path"], "user-agent": head["headers"].get("user-agent"),
                         "cookie": head["headers"].get("cookie")})
    assert head["headers"].get("cookie") == "E2E-CK=1"
    assert head["headers"].get("user-agent") == "FakeUA/9.9"  # addUri header 选项原样到达源站


async def test_02_reconcile_completes_row_with_backfill(hub, e2e):
    """第 3 项：reconcile 把 queued 收口成 done，回填 size_done/size_total/finished_at。

    真机分歧（记录，不在本提交修）：生产的问法 system.multicall + aria2.tellDownloadResult
    在真 aria2 上整段不可达——listMethods 无 tellDownloadResult，multicall 拒绝前导 token，
    子调用形状也与实现的解析假设不符。本用例先把这些原始响应打进证据，再断言 reconcile
    仍经文件兜底完成收口、回填值与真机 tellStatus(complete) 一致。"""
    src = source_file("t2")
    c = live_cfg("t2")
    e2e["dirs"].append(c.dir)
    driver = LiveDlDriver(files={"FID-T2": src}, base=hub.docker_base)
    item = dl.DownloadItem(fid="FID-T2", name="第2集.mkv", size=src["size"],
                           local_path=Path(c.dir) / "动漫" / "第2集.mkv")
    lines = await dl.download_items(driver, [item], c, log=lambda *a: None, task_id=9602,
                                    taskname="E2E对账", account_id=7, driver_key="fake")
    assert lines[0].startswith("✅")
    gid = snapshot(9602)[0]["ref_id"]
    e2e["gids"].append(gid)
    e2e["refs"].append((gid, "aria2"))

    ev("真机 listMethods 含 tellDownloadResult? ", "aria2.tellDownloadResult" in
       json.dumps(rpc("system.listMethods"), ensure_ascii=False))
    as_prod = rpc("system.multicall", [{"methodName": "aria2.tellDownloadResult", "params": [gid]}])
    ev("按生产原样发的 multicall（前导 token）响应", as_prod)
    assert "error" in as_prod  # 真机直接拒绝该载荷 → reconcile 的 RPC 结果分支拿不到任何 struct

    struct = wait_result(gid, "complete")
    ev("对账前 daemon 端 tellStatus(complete)", struct)
    assert await dl.aria2_status(c) == [] or gid not in {j["id"] for j in await dl.aria2_status(c)}

    before = ref_row(gid)
    assert before["status"] == "queued"
    ev("对账前账本行", before)
    await hist.reconcile(c)
    after = ref_row(gid)
    ev("对账后账本行", after)
    assert after["status"] == "done"
    assert after["size_done"] == src["size"] == int(struct["completedLength"])
    assert after["size_total"] == src["size"] == int(struct["totalLength"])
    assert after["finished_at"] is not None and not after["error"]


async def test_03_reconcile_gid_dropped_falls_back_to_stat(hub, e2e):
    """第 4 项：removeDownloadResult 丢弃 gid 后，reconcile 只能靠 os.stat 兜底判 done（RPC 已问不到）。"""
    src = source_file("t3")
    c = live_cfg("t3")
    e2e["dirs"].append(c.dir)
    driver = LiveDlDriver(files={"FID-T3": src}, base=hub.docker_base)
    item = dl.DownloadItem(fid="FID-T3", name="第3集.mkv", size=src["size"],
                           local_path=Path(c.dir) / "综艺" / "第3集.mkv")
    lines = await dl.download_items(driver, [item], c, log=lambda *a: None, task_id=9603,
                                    taskname="E2E兜底", account_id=7, driver_key="fake")
    gid = snapshot(9603)[0]["ref_id"]
    e2e["gids"].append(gid)
    e2e["refs"].append((gid, "aria2"))
    assert lines[0].startswith("✅")
    wait_result(gid, "complete")

    assert rpc("aria2.removeDownloadResult", gid).get("result") == "OK"
    gone = rpc("aria2.tellStatus", gid)
    ev("丢弃 gid 后 tellStatus 原始响应", gone)
    assert "error" in gone and gone["error"].get("code")  # RPC 层确实问不到了
    assert Path(ref_row(gid)["dest_path"]).exists()  # 文件在：兜底有据可依
    _reset_to_queued(gid)  # 按题设把行复位成非终态（scratch 库直接 UPDATE）
    assert ref_row(gid)["status"] == "queued"

    await hist.reconcile(c)
    after = ref_row(gid)
    ev("兜底对账后账本行", after)
    assert after["status"] == "done"
    assert after["size_done"] == src["size"]  # done 由 stat 的真实字节回填


async def test_04_aria2_retry_relands_at_original_dest(hub, e2e, monkeypatch):
    """第 5 项：aria2 模式下 failed 终态行经 retry_record 重投真 daemon，落回原 dest_path，且新增行不动旧行。"""
    src = source_file("t4")
    c = live_cfg("t4")
    e2e["dirs"].append(c.dir)
    dest = Path(c.dir) / "旧剧集" / "重下.mkv"
    old_ref = "f" * 16  # 假 gid 形状：查无此结果，行本身只是失败记录载体
    rid = hist.start(source="aria2", ref_id=old_ref, task_id=9604, taskname="E2E重下", filename="重下.mkv",
                     dest_path=str(dest), size_total=src["size"], fid="FID-T4", driver_key="fake", account_id=None)
    hist.finish(old_ref, source="aria2", status="failed", error="HTTP 403")
    old_before = hist.get_record(rid)

    LiveDlDriver.default_files = {"FID-T4": src}  # retry_record 内部按 DRIVERS 无参构造驱动，配置走类属性
    LiveDlDriver.default_base = hub.docker_base
    from backend import drivers
    monkeypatch.setitem(drivers.DRIVERS, "fake", LiveDlDriver)
    monkeypatch.setattr(dl, "_account_for", lambda rec: _Acc())

    msgs = []
    await dl.retry_record(old_before, c, log=lambda lvl, m: msgs.append((lvl, m)))
    ev("重下日志行", msgs)
    assert any(lvl == "info" and "🔁" in m for lvl, m in msgs)

    rows = snapshot(9604)
    assert len(rows) == 2, "重下必须新增一条账本行，不得改写旧行"
    new = rows[-1]
    assert new["ref_id"] != old_ref and GID_RE.fullmatch(new["ref_id"])
    assert new["status"] == "queued" and new["source"] == "aria2"
    assert new["dest_path"] == str(dest) and new["fid"] == "FID-T4" and new["size_total"] == src["size"]
    e2e["gids"].append(new["ref_id"])
    e2e["refs"].append((new["ref_id"], "aria2"))
    ev("重下新行", new)

    struct = wait_result(new["ref_id"], "complete")
    ev("重下 tellDownloadResult", struct)
    assert dest.exists() and hashlib.md5(dest.read_bytes()).hexdigest() == src["md5"]

    old_after = hist.get_record(rid)
    assert old_after == old_before  # 旧行一字未动


async def test_05_emby_refresh_wired_on_success(hub, e2e):
    """第 6 项：DownloadSettings.emby_url 指向本地 stub，投递成功的下载触发 POST /emby/Library/Refresh。

    这只证明「接线」——下载成功路径确实按配置向 Emby 端点发了带 api_key 的正确请求；
    stub 只记录请求，不代表真 Emby 的媒体库语义。"""
    src = source_file("t5")
    c = live_cfg("t5", emby_url=hub.local_base, emby_token="EMBY-E2E-TOKEN")
    e2e["dirs"].append(c.dir)
    driver = LiveDlDriver(files={"FID-T5": src}, base=hub.docker_base)
    item = dl.DownloadItem(fid="FID-T5", name="第5集.mkv", size=src["size"],
                           local_path=Path(c.dir) / "影视" / "第5集.mkv")
    lines = await dl.download_items(driver, [item], c, log=lambda *a: None, task_id=9605,
                                    taskname="E2EEmby", account_id=7, driver_key="fake")
    assert lines[0].startswith("✅")
    gid = snapshot(9605)[0]["ref_id"]
    e2e["gids"].append(gid)
    e2e["refs"].append((gid, "aria2"))

    posts = [r for r in hub.records if r["method"] == "POST"]
    ev("Emby stub 收到的请求行", posts)
    assert len(posts) == 1
    hit = posts[0]
    assert hit["path"] == "/emby/Library/Refresh?api_key=EMBY-E2E-TOKEN"
    ev("落盘旁证", wait_result(gid, "complete").get("status"))
