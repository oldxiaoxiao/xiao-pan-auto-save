# 小盘自动转存 0.4.0 · 知道有没有新版本，也知道该不该装

> 路线图「正式分发与更新」的第一刀：**把"有没有新版本"查清楚，把"下载到的包是不是官方那个"验明白，升级前先留一条退路。**
>
> 仍然**不自动下载、不自动替换**——是否升级由人决定。

## 版本检测

「设置 → 客户端」新增版本区块：显示当前版本，点「检查更新」与 GitHub 上的最新发布版比对（`GET /api/update/check`）。

- 有新版本时给出**本平台**的包：完整版（自带 aria2）与精简版**两个都列**，当前变体排在前面，但不替用户做这个选择——有人正是想从精简版换到完整版。
- 桌面端**不出 Linux 包**：Linux 上如实说明，并提示 Compose 部署改为更换镜像 tag，而不是给一个下下来装不上的链接。
- 结果**缓存 6 小时**（按钮可强制刷新）。GitHub 未认证接口限流 60 次/小时，缓存既是礼貌也是必要。
- **查不到时只显示原因**（离线、接口限流、返回内容异常），不弹错误、不反复重试。这条是硬约束：断网本来就够让人烦了，不该再叠一个与用户无关的堆栈。

## 升级前备份

`POST /api/update/prebackup` 与界面上的「升级前备份」按钮：把数据库快照到数据目录的 `backups/`，并返回路径与大小。

新版本若带数据结构变更，这一步就是退路。**它替代不了完整备份**（下载文件、密钥文件不在库里），重要数据仍建议走「设置 → 备份」的导出。

## 下载校验

每个 Release 都带 `SHA256SUMS`。界面在给出下载链接的同时，给出**当前平台的原生校验命令**：

| 平台 | 命令 |
|---|---|
| Windows | `certutil -hashfile <包名>.zip SHA256` |
| macOS | `shasum -a 256 <包名>.zip` |
| Linux / Compose | `sha256sum <包名>.zip` |

ZIP 用户手上没有源码，给一条 `python scripts/...` 等于没给——所以按平台给系统自带命令。

源码 / Compose 用户可用新脚本一次核对整个目录：

```bash
python scripts/verify_release.py 下载目录/          # 自动找同目录的 SHA256SUMS
python scripts/verify_release.py 包.zip --sums SHA256SUMS
python scripts/verify_release.py 包.zip --expected <64位摘要>
```

退出码：`0` 全部一致，`1` 存在不一致或校验和里没有这一项，`2` 参数/环境问题（没找到文件、没给校验依据）。
**校验不通过就不要解压安装**——这正是本脚本存在的唯一理由。

## 测试与验证

- 新增 `backend/tests/test_update_service.py`（21 例）与 `test_verify_release.py`（7 例）。
  出网点收在单一接缝 `_fetch_json` 上，**用例全部注入假数据、不联网**：让 CI 去撞 60 次/小时的限流只会得到随机红。
- 后端：ruff 通过，**628 passed / 1 skipped**（较 0.3.2 增加 28 例）。前端：prettier / eslint **0 error** / vue-tsc 通过。
- 真实端到端（本机联网，非 mock）：
  - 真实调用 GitHub 接口：`latest=0.3.2`、`has_update=false`、`platform=macos-arm64`、`variant=slim`，
    资产正确过滤出 `macos-arm64` 两个变体并给出 `SHA256SUMS` 地址。
  - 真实 HTTP（uvicorn + curl）：`/api/update/check?force=1` 同上；`POST /api/update/prebackup`
    落出真实快照 `xiao_pan-20261013-013851.db`（126,976 字节）。
  - 真实包校验：`verify_release.py` 对 24MB 的 `0.1.1-macos-arm64.zip` 算出摘要并与 `SHA256SUMS` 一致（退出 0）；
    追加一个字节后报「不一致」并退出 1。

## 已知边界（本期未做）

- **签名与公证仍未做**：Windows 无发行者签名，macOS 无公证，首次打开仍会有系统拦截提示。做签名时，
  随包 aria2c 及其 dylib 必须一并签名，否则 macOS 公证会失败。
- **没有 Windows 安装器**，仍是解压即用；也没有"下载后自动替换并重启"的能力。
- 版本检测读的是公共 GitHub 接口，**不发送任何本机数据**；若在隔离内网运行，会如实显示"无法连接更新服务"。
- **FR-11（SQLite WAL 与写保护）**继续顺延至 v0.4 后续。仍为单网盘（夸克）可用；桌面端不出 Linux 包。

## 升级注意

1. 本版新增接口与界面改动，**未改动数据库结构**，升级向下兼容。
2. 升级客户端前建议点一次「升级前备份」。数据目录（`xiao_pan.db`、`logs/`、`backups/`、`downloads/`）不会被升级覆盖。
3. Compose 用户：版本检测可用，但参照的是镜像 tag；升级请更换固定版本镜像。

## 许可说明

完整版（带 `-aria2` 后缀）随包的 aria2 为 **GPL-2.0-or-later**（本项目为 AGPL-3.0-only）。若你更倾向于不随包 GPL 二进制，请选择**精简版**。
