# 配置参考

[项目首页](../README.md) · [功能手册](features.md) · [Compose 部署](deployment.md)

配置分两层：环境变量决定启动环境，Web 设置持久保存在 SQLite 中。修改 `.env` 后重建容器；修改 Web 设置后按页面保存。桌面客户端会自动设置自己的数据目录和启动鉴权，不需要复制 Compose 的 `.env`。

## 环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `DATA_DIR` | 源码目录 `data/`；容器 `/app/data` | 数据库、日志、备份和默认下载根目录 |
| `WEBUI_USERNAME` | `admin` | 管理登录用户名 |
| `WEBUI_PASSWORD` | 空 | 空为免登录；非空启用网页登录和 HTTP Basic |
| `API_TOKEN` | 空 | 外部 API 的备用 Token；也可在界面创建多 Token |
| `CRONTAB` | `0 9 * * *` | 尚未在数据库保存规则时的默认调度 |
| `PROXY` | 空 | 网盘驱动 HTTP 代理；不是所有外部集成的统一代理 |
| `REQUEST_TIMEOUT` | `30` | 网盘 HTTP 请求超时秒数 |
| `TZ` | Compose 为 `Asia/Shanghai` | 进程时区，桌面默认使用系统时区 |
| `XIAO_PAN_DESKTOP_DATA_DIR` | 系统用户数据目录 | 桌面数据目录覆盖值 |

以下是 Compose 插值变量，不是 Web 设置：`XIAO_PAN_VERSION=0.1.1`、`WEBUI_BIND=127.0.0.1`、`WEBUI_PORT=8432`、`ARIA2_SECRET`。完整模板见仓库 `.env.example`。`REQUEST_TIMEOUT` 若用于 Compose，需自行添加到 service 的 `environment`。

## 账号 Cookie

在账号页添加夸克 Web Cookie，健康检查确认账号有效。列表只显示掩码；编辑时 Cookie 留空表示保持原值。当前不提供扫码登录、密码登录或自动获取 Cookie。

签到需要有效移动端参数，可附在 Cookie 后：

```text
web_cookie#kps=...&sign=...&vcode=...
```

普通转存不要求这些签到参数。Cookie 过期、账号配额不足、服务端限流或权限变化都可能让真实运行失败，需要查看日志并更新凭证。

## 调度与新建默认

- 全局调度使用 5 段 cron，默认每日 09:00；设置页可修改并立即重新调度。
- 单任务可选每 N 分钟或独立 cron；使用有效独立规则的任务不被全局扫描重复触发。
- 执行方式分 `follow`、`manual`、`once`，详见[功能手册](features.md)。
- 星期过滤及截止日期按运行机器时区解释。
- “运行通知”“自动签到”是独立全局开关。签到开关也控制主调度中的账号刷新，手动健康检查仍可用。
- “新建默认”控制保存路径根、下载到本地、执行方式、文件范围、画质和子目录过滤，只影响之后创建的任务。

新建网页表单默认开启本地下载与子目录过滤；如只想云端转存，先关闭新建默认中的下载。外部 API 省略 `auto_download` 时仍按后端默认 `true` 处理，不跟随网页表单的所有默认设置。

## 下载设置

| 项目 | 默认/说明 |
|---|---|
| 模式 | `builtin`；可选 `aria2` |
| 下载根目录 | 留空使用 `DATA_DIR/downloads`；推荐填写绝对路径 |
| 并发数 | `2`，仅控制内置下载器 |
| 历史保留 | 最近 90 天；可选 30/180 天或永久保留 |
| Aria2 地址 | 主机:端口或完整 JSON-RPC URL |
| Aria2 密钥 | 发送为 JSON-RPC `token:` 参数 |
| 暂停投递 | 仅 Aria2 生效 |
| Emby 地址/Token | 可选，下载流程触发媒体库刷新请求 |

Compose 的路径是容器路径，客户端的路径是电脑本地路径。Aria2 与后端必须看到同一目标文件；若接入远程 Aria2，不能默认认为远程文件也能被本地历史页检查。内置 `.part` 是临时写入保护，不是断点续传。

Emby 请求是否被接收与媒体库是否成功完成扫描是不同状态；请在 Emby 中验证扫描结果。

## 搜索源

出厂配置包含 PanSou 公共站 `https://so.252035.xyz` 和夸克搜 `https://kkso.net`。这些地址是默认配置，不保证长期可达。可按协议添加多个实例、启用/停用，并在新建任务时切换单源。

| 协议 | 需要填写 |
|---|---|
| PanSou | 实例名称、服务器地址 |
| 夸克搜（kkso） | 实例名称、服务器地址 |
| CloudSaver | 自建服务器地址、用户名、密码；Token 由登录获取 |

未配置、协议错误、请求超时和部分引擎失败会在搜索结果中显示原因。搜索结果来自外部站点，创建前检查分享内容和有效性。

## 通知渠道

在设置 → 通知渠道填写密钥并保存，再发送单渠道测试。测试使用已保存的配置。填写必需键会隐式启用渠道，显式关闭写入 `<渠道名>_ENABLE=false`。

| 常用渠道 | 主要配置键 |
|---|---|
| Bark | `BARK_PUSH` |
| Server 酱 | `PUSH_KEY` |
| PushPlus | `PUSH_PLUS_TOKEN` |
| Telegram | `TG_BOT_TOKEN`、`TG_USER_ID` |
| 钉钉 | `DD_BOT_TOKEN`、`DD_BOT_SECRET` |
| 飞书 | `FSKEY` |
| 企业微信机器人/应用 | `QYWX_KEY` / `QYWX_AM` |
| PushDeer | `DEER_KEY` |
| ntfy | `NTFY_TOPIC`，可选服务器与鉴权配置 |
| 自定义 Webhook | `WEBHOOK_URL`、`WEBHOOK_METHOD`，可选请求模板 |

后端实现 24 种渠道，页面展示常用渠道；完整协议键以 `backend/services/notify_service.py` 的 `CHANNELS` 注册表和发送函数为准。其余配置可通过 `PUT /api/settings/push_config` 写入完整配置。未进行真实账号测试的渠道不能视为当前环境已可用。

## 管理鉴权与外部 API

网页登录会话有效期 7 天，后端重启或修改密码会使会话失效；客户端每次启动自动登录。外部 Token API 不要求额外 Web 密码，但该 Token不能访问管理接口。

```bash
curl -u admin http://127.0.0.1:8432/api/settings
# curl 会提示输入密码
```

对外 API 推荐使用 `Authorization: Bearer <token>`。查询参数 Token 用于旧脚本兼容，可能进入代理日志。Web Token 的完整值只在创建时展示一次，请保存；删除 Token 会立即使使用它的外部脚本失效。

## 魔法匹配与迁移

支持 `$TV` 与 `{TASKNAME}`、`{E}`、`{II}`、`{EXT}`、`{DATE}`、`{S}`、`{SXX}` 等变量。先使用试跑验证匹配和重命名，避免误处理云端文件。旧配置导入规则及不兼容项见[迁移手册](migrate-from-quark-auto-save.md)。
