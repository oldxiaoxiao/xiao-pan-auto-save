# 如何为新网盘编写驱动

本文档面向想给 xiao-pan-auto-save 增加网盘支持的开发者。系统的追更引擎只依赖
`backend/drivers/base.py` 中的 `CloudDrive` 抽象接口，**新增一个网盘 = 新增一个文件**，
无需改动引擎、API 或前端（前端驱动矩阵由注册表自动生成）。

## 1. 五原语：驱动必须实现的最小闭环

在 `backend/drivers/` 新建 `<key>.py`，继承 `CloudDrive`，注册表会在启动时自动发现：

```python
class MyDriver(CloudDrive):
    key = "mypan"                       # 唯一标识
    name = "我的网盘"
    share_domains = ["pan.mypan.com"]   # 任务按分享链接域名自动路由
    supported = True
    capability = {"rename", "delete", "sign"}  # 逐项声明可选能力

    def parse_share(self, url) -> ShareRef: ...            # 解析 pwd_id/提取码/深链子目录
    async def list_share(self, ref, path) -> list[FsItem]: # 列分享内目录（换取 stoken，翻页取全）
    async def list_dir(self, path) -> list[FsItem]:        # 列自己的盘（路径→fid 缓存）
    async def save(self, items, dest_path, ref) -> SaveResult:  # 转存核心，顺序返回 saved
    async def ensure_dir(self, path) -> str:               # 目录不存在则递归创建
```

约定（务必遵守，否则引擎行为会错）：

- `FsItem.token`：承载"转存该分享文件所需的私有凭证"（如夸克的 `share_fid_token`），
  引擎会把它原样传回 `save()`。
- `list_share` 失败语义：**永久失效抛 `ShareBanned(message)`**（任务被标记 `shareurl_ban`
  并通知）；**网络/临时异常抛 `ShareUnavailable`**（本次跳过，下次重试）。这个区分是
  防误杀链接的核心，照抄 `quark.py::_get_stoken` 的三态判定。
- `list_share(ref, path)` 的 `path` 是引擎递归用的分享内路径（`""`=根，`"/剧名/4K"`=子目录）。
  驱动在 `ref.extra` 里自维护 路径→fid 缓存（参照夸克实现：列父目录时把子目录登记进缓存）。
- `save()` 返回的 `saved` 列表**顺序必须与入参 items 一一对应**，引擎据此做转存后重命名；
  若网盘异步任务只能返回无序 fid 集合，请在驱动内部完成顺序对齐（或返回空并置 ok=True，
  引擎会跳过重命名）。
- 单次转存有数量上限的（夸克 100/批），在驱动 `save()` 内部分批，引擎不感知。
- 可选能力：实现 `rename/delete_items/sign/account_info/move`，并在 `capability` 里声明。
  未声明的方法基类会抛 `CapabilityError`，引擎自动降级（例如无 `delete` 能力时
  "子目录重存模式"退化为跳过）。

## 2. 辅助设施

- `DriveHttpClient`（drivers/http.py）：带 1/2/4s 退避重试的 httpx 封装、请求头管理、日志回调。
- 日志：驱动构造时收到 `cookie/proxy/index`；运行日志由引擎/服务层统一发布到 SSE+文件。
- 风控参数（UA、签名、随机延迟等）全部封装在驱动内部，不允许泄漏到引擎。

## 3. 自测要求

- `backend/tests/test_engine.py` 的行为全部由假驱动覆盖，新驱动不要求改引擎测试；
- 请参照 `test_quark.py` 用 `Recorder`/`httpx.MockTransport` mock 全部 HTTP 交互，
  断言：请求参数还原、翻页聚合、token 三态、分批转存、错误路径；**不允许打真实 API**。
- 真机验证：`python scripts/cli_save.py --cookie ... --shareurl ... --savepath ... --preview`。

## 4. 待实现驱动接入要点（按难度排序）

| 驱动 | key | 难度 | 关键协议 | 主要风险 |
|---|---|---|---|---|
| 迅雷云盘 | xunlei | ★☆☆ | `share/token`(pwd_id+passcode→stoken) → `share/detail` 分页 → `drive/saveas`(fid_list+fid_token_list)，**阿里系协议、与夸克高度相似** | 需会员才有足够转存配额 |
| 123云盘 | pan123 | ★☆☆ | `share/get/share`+`get/file` 分页 → `share/save/copy` 返回 TaskID → `user/task/list` 轮询 | 请求体 sign 头算法曾变更多次，需抓包跟进 |
| AList/OpenList | alist | ★☆☆ | REST `/api/fs/{list,add,rename,move,delete}`，路径即 fid | 不代理"别人的分享链接"，适合做通用目标盘 |
| 天翼云盘 | ctyun | ★★☆ | share 接口 `GetShareResourceByTime` → `BatchCopyFile`；ListDirectory/CreateFolder | HMAC Signature 请求头（密钥在 JS），token 时效短 |
| 移动云盘 | cmcc | ★★☆ | `linkID+passcode` 分享解析 → `pan/personal/batch/copy` | Bearer JWT + x-Signature 双签名，RSA 登录流程复杂 |
| 百度网盘 | baidu | ★★☆ | `share/confirm` → `share/list`(shareid+uk) → `share/transfer`+`share/async` 轮询；xpan/file+multimedia 管理 | BDUSS 风控、限速、接口版本混乱 |
| 阿里云盘 | aliyun | ★★★ | `share_link/get_share_token` → `file/list_by_share` → `file/batch` 转存 | Web token 大面积失效、signatureUrl 风控强；建议走开放平台或 AList 代理 |
| 115 | pan115 | ★★★ | 开放平台 OAuth2（自己盘：files.find/add_fold/rename/move/delete）；**分享转存无官方 API**，需 Web 逆向 | 开放平台限频严格；自动化签到存在合规风险 |

每个骨架文件头部有完整 `# TODO(driver)` 注记（端点、鉴权、字段、轮询方式），实现时
从对应文件开始，把 `supported = False` 改为 `True` 并补齐方法即可，前端"即将支持"
徽章会自动变为可用。

## 5. 能力矩阵建议

第一期优先落地 **迅雷或123云盘**：协议结构最接近夸克，能最大化复用
`parse_share/stoken/saveas/轮询` 的模式，也顺势验证 `CloudDrive` 抽象是否干净——
如果实现中觉得接口别扭，欢迎提 issue，抽象层应当为网盘协议服务而不是反过来。
