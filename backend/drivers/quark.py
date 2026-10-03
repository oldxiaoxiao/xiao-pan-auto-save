"""夸克网盘驱动（完整实现）。

端点与参数还原自 quark-auto-save 项目实际验证过的请求格式：
- PC: https://drive-pc.quark.cn   App: https://drive-m.quark.cn
- 分享体系：pwd_id + 提取码 → stoken；每个分享文件携带 share_fid_token，
  转存需 fid_list + fid_token_list 一一对应。
- Cookie 尾部 `#kps=..&sign=..&vcode=..` 提供移动端签名参数（签到 + 分享接口匿名化）。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
import urllib.parse
from typing import Any

from .base import (
    AccountInfo,
    CloudDrive,
    DriveError,
    FsItem,
    SaveResult,
    ShareBanned,
    ShareRef,
    ShareUnavailable,
    SignResult,
)
from .http import DriveHttpClient

BASE_URL = "https://drive-pc.quark.cn"
BASE_URL_APP = "https://drive-m.quark.cn"
PAN_URL = "https://pan.quark.cn"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 "
    "Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"
)

# 移动端签名参数（附在走 App 域名的 share 接口上，替代 Cookie 鉴权）
_MOBILE_PARAMS = {
    "device_model": "M2011K2C",
    "entry": "default_clouddrive",
    "_t_group": "0%3A_s_vp%3A1",
    "dmn": "Mi%2B11",
    "fr": "android",
    "pf": "3300",
    "bi": "35937",
    "ve": "7.4.5.680",
    "ss": "411x875",
    "mi": "M2011K2C",
    "nt": "5",
    "nw": "0",
    "kt": "4",
    "pr": "ucpro",
    "sv": "release",
    "dt": "phone",
    "data_from": "ucapi",
    "app": "clouddrive",
    "kkkk": "1",
}

_MEMBER_TYPE_NAMES = {
    "NORMAL": "普通用户",
    "EXP_SVIP": "88VIP",
    "SUPER_VIP": "SVIP",
    "Z_VIP": "SVIP+",
}

_MPARAM_VALUE = r"[a-zA-Z0-9%+/=]+"


def _match_mparam(cookie: str) -> dict[str, str] | None:
    """从 Cookie 尾部解析 kps/sign/vcode（格式 `...#kps=..&sign=..&vcode=..`，分隔符 & 或 ;）。"""
    kps = re.search(rf"(?<!\w)kps=({_MPARAM_VALUE})[;&]?", cookie)
    sign = re.search(rf"(?<!\w)sign=({_MPARAM_VALUE})[;&]?", cookie)
    vcode = re.search(rf"(?<!\w)vcode=({_MPARAM_VALUE})[;&]?", cookie)
    if kps and sign and vcode:
        return {
            k: v.group(1).replace("%25", "%") for k, v in (("kps", kps), ("sign", sign), ("vcode", vcode))
        }
    return None


def _anti_detect_params() -> dict[str, Any]:
    """模拟转存耗时的 __dt/__t 参数。"""
    return {
        "__dt": int(random.uniform(1, 5) * 60 * 1000),
        "__t": time.time(),
    }


class QuarkDriver(CloudDrive):
    key = "quark"
    name = "夸克网盘"
    share_domains = ["pan.quark.cn", "drive.quark.cn"]
    supported = True
    capability = {"rename", "delete", "move", "mkdir", "sign", "recycle", "account", "preview", "download"}
    UA = USER_AGENT  # 下载直链需原样携带

    def __init__(self, cookie: str = "", proxy: str = "", index: int = 0):
        super().__init__(cookie, proxy, index)
        self.mparam = _match_mparam(self.cookie)
        self.client = DriveHttpClient(
            headers={
                "cookie": self.cookie,
                "content-type": "application/json",
                "user-agent": USER_AGENT,
            },
            proxy=proxy,
        )
        self.savepath_fid: dict[str, str] = {"/": "0"}

    # ------------------------------------------------------------------
    # 基础请求：风控路由
    # ------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict:
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": "", **(params or {})}
        headers: dict[str, str] = {}
        # 有移动端签名参数时，share 类接口改走 App 域名并匿名化（去掉 Cookie）
        if self.mparam and "share" in url and url.startswith(BASE_URL):
            url = url.replace(BASE_URL, BASE_URL_APP)
            params.update(_MOBILE_PARAMS)
            params.update(self.mparam)
            headers["cookie"] = None  # type: ignore[assignment]

        try:
            resp = await self.client.request(method, url, params=params, headers=headers or None, json=json)
        except ConnectionError as exc:
            raise ShareUnavailable(str(exc)) from exc

        if not isinstance(resp, dict):
            raise ShareUnavailable(f"响应解析异常: {resp!r}")
        return resp

    @staticmethod
    def _check_ok(resp: dict, action: str) -> dict:
        """统一业务判定：code==0 成功，否则抛 DriveError。"""
        if resp.get("code") == 0:
            return resp
        raise DriveError(f"{action}失败: code={resp.get('code')} {resp.get('message', '')}")

    # ------------------------------------------------------------------
    # 分享解析与 stoken
    # ------------------------------------------------------------------
    def parse_share(self, url: str) -> ShareRef:
        pwd_match = re.search(r"/s/(\w+)", url)
        if not pwd_match:
            raise DriveError(f"无法解析分享链接: {url}")
        pwd_id = pwd_match.group(1)
        passcode_match = re.search(r"pwd=(\w+)", url)
        passcode = passcode_match.group(1) if passcode_match else ""
        # 深链子目录：#/list/share/<32位fid>-<名称>，名称 URL 编码且 "*101" 代表 "-"
        pdir_fid, sub_names = "0", []
        for fid, raw_name in re.findall(r"/(\w{32})-?([^/]+)?", url):
            name = urllib.parse.unquote(raw_name or "").replace("*101", "-")
            pdir_fid, *_ = [fid]
            sub_names.append(name)
        return ShareRef(
            url=url,
            pwd_id=pwd_id,
            passcode=passcode,
            pdir_fid=pdir_fid,
            sub_names=sub_names,
            extra={"path_fids": {"": pdir_fid}},
        )

    async def _get_stoken(self, ref: ShareRef) -> str:
        """换取 stoken；三态：正常 / 网络异常(可重试) / 永久失效。"""
        if ref.extra.get("stoken"):
            return ref.extra["stoken"]
        resp = await self._request(
            "POST",
            f"{BASE_URL}/1/clouddrive/share/sharepage/token",
            json={"pwd_id": ref.pwd_id, "passcode": ref.passcode},
        )
        status = resp.get("status")
        if status == 200 and resp.get("code") == 0:
            stoken = resp["data"]["stoken"]
            ref.extra["stoken"] = stoken
            return stoken
        message = str(resp.get("message", ""))
        if not status or status >= 500 or "request error" in message.lower():
            # 与原项目一致：status=500/网络错误视为临时异常，本次跳过、不判失效
            raise ShareUnavailable(message or "网络异常(status=500)")
        raise ShareBanned(message or "分享链接已失效")

    async def _get_detail(self, ref: ShareRef, pdir_fid: str) -> list[FsItem]:
        """分享文件列表，自动翻页。share_fid_token 存入 FsItem.token。"""
        stoken = await self._get_stoken(ref)
        items: list[FsItem] = []
        page = 1
        while True:
            resp = await self._request(
                "GET",
                f"{BASE_URL}/1/clouddrive/share/sharepage/detail",
                params={
                    "pwd_id": ref.pwd_id,
                    "stoken": stoken,
                    "pdir_fid": pdir_fid or "0",
                    "force": 0,
                    "_page": page,
                    "_size": 50,
                    "_fetch_banner": 0,
                    "_fetch_total": 1,
                    "_fetch_share": 0,
                    "_sort": "file_type:asc,updated_at:desc",
                    "ver": 2,
                },
            )
            self._check_ok(resp, "获取分享列表")
            data = resp.get("data") or {}
            batch = data.get("list") or []
            if not batch:
                break
            for f in batch:
                items.append(self._to_fsitem(f))
            total = (resp.get("metadata") or {}).get("_total", 0)
            if total and len(items) >= total:
                break
            page += 1
        return items

    @staticmethod
    def _to_fsitem(f: dict) -> FsItem:
        return FsItem(
            fid=f["fid"],
            name=f["file_name"],
            is_dir=bool(f.get("dir")),
            size=int(f.get("size") or 0),
            mtime=float(f.get("updated_at") or 0),
            token=f.get("share_fid_token") or "",
            extra={"pdir_fid": f.get("pdir_fid"), "obj_category": f.get("obj_category", "")},
        )

    # ------------------------------------------------------------------
    # 追更五原语
    # ------------------------------------------------------------------
    async def list_share(self, ref: ShareRef, path: str = "") -> list[FsItem]:
        path_fid = ref.extra["path_fids"]
        if path not in path_fid:
            parent = path.rsplit("/", 1)[0]
            parent_items = await self.list_share(ref, parent)
            name = path.rsplit("/", 1)[1]
            target = next((i for i in parent_items if i.is_dir and i.name == name), None)
            if target is None:
                return []
            path_fid[parent + "/" + name] = target.fid
        items = await self._get_detail(ref, path_fid[path])
        for i in items:
            if i.is_dir:
                path_fid.setdefault(f"{path}/{i.name}", i.fid)
        return items

    async def _resolve_fid(self, path: str) -> str | None:
        """批量路径取 fid（带缓存），每批 50。"""
        if path in self.savepath_fid:
            return self.savepath_fid[path]
        resp = await self._request(
            "POST",
            f"{BASE_URL}/1/clouddrive/file/info/path_list",
            json={"file_path": [path], "namespace": "0"},
        )
        self._check_ok(resp, "解析路径")
        for entry in resp.get("data") or []:
            if entry.get("file_path") == path:
                self.savepath_fid[path] = entry["fid"]
                return entry["fid"]
        return None

    async def list_dir(self, path: str) -> list[FsItem]:
        fid = await self._resolve_fid(path)
        if not fid:
            return []
        return await self._ls_by_fid(fid)

    async def list_dir_children(self, fid: str) -> list[FsItem]:
        return await self._ls_by_fid(fid)

    async def _ls_by_fid(self, fid: str) -> list[FsItem]:
        items: list[FsItem] = []
        page = 1
        while True:
            resp = await self._request(
                "GET",
                f"{BASE_URL}/1/clouddrive/file/sort",
                params={
                    "pdir_fid": fid,
                    "_page": page,
                    "_size": 50,
                    "_fetch_total": 1,
                    "_fetch_sub_dirs": 0,
                    "_sort": "file_type:asc,updated_at:desc",
                    # 不带此参数，违规文件名会被服务端替换成 ***
                    "fetch_risk_file_name": 1,
                    "fetch_all_file": 1,
                },
            )
            self._check_ok(resp, "获取目录列表")
            batch = (resp.get("data") or {}).get("list") or []
            if not batch:
                break
            for f in batch:
                items.append(
                    FsItem(
                        fid=f["fid"],
                        name=f["file_name"],
                        is_dir=bool(f.get("dir") or f.get("file_type") == 0),
                        size=int(f.get("size") or 0),
                        mtime=float(f.get("updated_at") or 0),
                        extra={"obj_category": f.get("obj_category", "")},
                    )
                )
            total = (resp.get("metadata") or {}).get("_total", 0)
            if total and len(items) >= total:
                break
            page += 1
        return items

    async def ensure_dir(self, path: str) -> str:
        fid = await self._resolve_fid(path)
        if fid:
            return fid
        resp = await self._request(
            "POST",
            f"{BASE_URL}/1/clouddrive/file",
            json={"pdir_fid": "0", "file_name": "", "dir_path": path, "dir_init_lock": False},
        )
        self._check_ok(resp, f"创建目录 {path}")
        fid = resp["data"]["fid"]
        self.savepath_fid[path] = fid
        return fid

    async def save(self, items: list[FsItem], dest_path: str, ref: ShareRef | None = None) -> SaveResult:
        if not items:
            return SaveResult(ok=True, saved=[])
        if ref is None:
            raise DriveError("夸克转存需要分享上下文 ShareRef(pwd_id/stoken)")
        stoken = await self._get_stoken(ref)
        to_pdir_fid = await self.ensure_dir(dest_path)
        saved: list[FsItem] = []
        message = ""
        # save_as_top_fids 单次最多返回 100 个，按 100/批转存
        for start in range(0, len(items), 100):
            batch = items[start : start + 100]
            resp = await self._request(
                "POST",
                f"{BASE_URL}/1/clouddrive/share/sharepage/save",
                params={"app": "clouddrive", **_anti_detect_params()},
                json={
                    "fid_list": [i.fid for i in batch],
                    "fid_token_list": [i.token for i in batch],
                    "to_pdir_fid": to_pdir_fid,
                    "pwd_id": ref.pwd_id,
                    "stoken": stoken,
                    "pdir_fid": "0",
                    "scene": "link",
                },
            )
            if resp.get("code") != 0:
                return SaveResult(ok=False, saved=saved, message=str(resp.get("message", "转存失败")))
            task_resp = await self._query_task(resp["data"]["task_id"])
            fids = ((task_resp.get("data") or {}).get("save_as") or {}).get("save_as_top_fids") or []
            for src, new_fid in zip(batch, fids, strict=False):
                saved.append(
                    FsItem(
                        fid=str(new_fid),
                        name=src.name,
                        is_dir=src.is_dir,
                        size=src.size,
                        mtime=src.mtime,
                        extra={"obj_category": src.extra.get("obj_category", "")},
                    )
                )
            if len(fids) != len(batch):
                message = f"部分转存未完成（{len(fids)}/{len(batch)}）"
        return SaveResult(ok=True, saved=saved, message=message)

    async def _query_task(self, task_id: str, max_wait: float = 300.0) -> dict:
        """轮询异步任务直到完成（data.status==2）或超时。"""
        deadline = time.time() + max_wait
        retry_index = 0
        while time.time() < deadline:
            resp = await self._request(
                "GET",
                f"{BASE_URL}/1/clouddrive/task",
                params={"task_id": task_id, "retry_index": retry_index, **_anti_detect_params()},
            )
            if resp.get("status") not in (200, None) and resp.get("code") != 0:
                return resp
            if (resp.get("data") or {}).get("status") == 2:
                return resp
            retry_index += 1
            await asyncio.sleep(0.5)
        raise DriveError(f"任务轮询超时: {task_id}")

    # ------------------------------------------------------------------
    # 可选能力
    # ------------------------------------------------------------------
    async def rename(self, item_or_fid: FsItem | str, new_name: str) -> None:
        fid = item_or_fid.fid if isinstance(item_or_fid, FsItem) else item_or_fid
        resp = await self._request(
            "POST", f"{BASE_URL}/1/clouddrive/file/rename", json={"fid": fid, "file_name": new_name}
        )
        self._check_ok(resp, f"重命名 {new_name}")

    async def delete_items(self, items: list[FsItem], purge: bool = True) -> None:
        """删除进回收站；purge 时再彻底删除（重存模式需要，回收站残留会阻塞同名建目录）。"""
        if not items:
            return
        resp = await self._request(
            "POST",
            f"{BASE_URL}/1/clouddrive/file/delete",
            json={"action_type": 2, "filelist": [i.fid for i in items], "exclude_fids": []},
        )
        self._check_ok(resp, "删除")
        await self._query_task(resp["data"]["task_id"])
        if not purge:
            return
        fid_set = {i.fid for i in items}
        record_ids: list[str] = []
        page = 1
        while len(record_ids) < len(fid_set) and page <= 20:
            recycle = await self._request(
                "GET",
                f"{BASE_URL}/1/clouddrive/file/recycle/list",
                params={"_page": page, "_size": 30},
            )
            rows = (recycle.get("data") or {}).get("list") or []
            if not rows:
                break
            record_ids.extend(r["record_id"] for r in rows if r.get("fid") in fid_set)
            page += 1
        if record_ids:
            resp = await self._request(
                "POST",
                f"{BASE_URL}/1/clouddrive/file/recycle/remove",
                json={"select_mode": 2, "record_list": record_ids},
            )
            self._check_ok(resp, "回收站彻底删除")

    async def move(self, items: list[FsItem], dest_path: str) -> None:
        to_pdir_fid = await self.ensure_dir(dest_path)
        resp = await self._request(
            "POST",
            f"{BASE_URL}/1/clouddrive/file/move",
            json={
                "filelist": [i.fid for i in items],
                "to_pdir_fid": to_pdir_fid,
                "exclude_fids": [],
                "action_type": 1,
            },
        )
        self._check_ok(resp, "移动")

    async def account_info(self) -> AccountInfo:
        info = AccountInfo(can_save="__uid" in self.cookie)
        resp = await self._request("GET", f"{PAN_URL}/account/info", params={"fr": "pc", "platform": "pc"})
        data = resp.get("data") or {}
        info.valid = bool(data)
        info.nickname = data.get("nickname") or data.get("member_type") or ""
        if info.valid and self.mparam:
            try:
                growth = await self._request(
                    "GET",
                    f"{BASE_URL_APP}/1/clouddrive/capacity/growth/info",
                    params={"pr": "ucpro", "fr": "android"},
                )
                data = growth.get("data") or {}
                info.total = int(data.get("total_capacity") or 0)
                info.member_type = _MEMBER_TYPE_NAMES.get(
                    data.get("member_type", ""), data.get("member_type", "")
                )
            except Exception:
                pass
        return info

    async def sign(self) -> SignResult:
        if not self.mparam:
            return SignResult(ok=False, message="Cookie 缺少移动端签名参数(#kps=..&sign=..&vcode=..)")
        await self._request(
            "GET",
            f"{BASE_URL_APP}/1/clouddrive/capacity/growth/info",
            params={"pr": "ucpro", "fr": "android"},
        )
        resp = await self._request(
            "POST",
            f"{BASE_URL_APP}/1/clouddrive/capacity/growth/sign",
            params={"pr": "ucpro", "fr": "android"},
            json={"sign_cyclic": True},
        )
        if resp.get("code") != 0:
            message = str(resp.get("message", ""))
            if "sign" in message.lower() or "已签到" in message:
                return SignResult(ok=True, message="今日已签到")
            return SignResult(ok=False, message=message or "签到失败")
        reward = int(((resp.get("data") or {}).get("sign_daily_reward")) or 0)
        return SignResult(ok=True, message=f"签到成功 +{reward / 1024 / 1024:.0f}MB", reward_bytes=reward)

    async def get_download_urls(self, fids: list[str]) -> tuple[list[dict], str]:
        """POST file/download 取直链；响应 Set-Cookie 拼成 cookie_str，直连下载需带
        该 Cookie + 驱动 UA（与原项目 aria2 插件协议一致）。"""
        resp, cookie_str = await self.client.request(
            "POST",
            f"{BASE_URL}/1/clouddrive/file/download",
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
            json={"fids": fids},
            with_cookies=True,
        )
        self._check_ok(resp, "获取下载直链")
        rows = [
            {
                "fid": d.get("fid"),
                "file_name": d.get("file_name", ""),
                "size": int(d.get("size") or 0),
                "download_url": d.get("download_url", ""),
            }
            for d in resp.get("data") or []
        ]
        return rows, cookie_str or self.cookie

    async def close(self) -> None:
        await self.client._client.aclose()
