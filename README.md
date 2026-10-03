# xiao-pan-auto-save

<p align="center">
  <img src="assets/logo.png" width="200" alt="xiao logo" />
</p>

多网盘分享链接 **自动转存 / 追更** 系统。给网盘账号配置 Cookie，系统按计划任务自动检查
分享链接更新，把新文件转存到指定目录并按规则重命名，全程推送通知。

> 业务思路脱胎于 [quark-auto-save](https://github.com/Cp0204/quark-auto-save)（AGPL-3.0），
> 因为原理项目页面和操作方式都很不习惯所以进行升级优化
> 但架构与界面全部重写：FastAPI + SQLite + Vue3，网盘操作抽象为可插拔驱动层。

![界面截图占位](docs/screenshot.png)

## 特性

- 🔌 **驱动抽象层**：任务按分享链接域名自动路由网盘驱动，加一个网盘 = 加一个文件
  （教程见 [docs/drivers.md](docs/drivers.md)）。当前已支持 **夸克网盘**，
  迅雷 / 123 / 百度 / 阿里 / 115 / 天翼 / 移动 / AList 驱动骨架已就位（即将支持）。
- 🔄 **追更引擎**：分享列表与目标目录差集比对只转新增；忽略扩展名去重（`01.mp4≈01.mkv`）；
  起始文件订阅（跳过老集数）；子目录追更（递归比对 / 删除重存两种模式）；链接失效自动标记并通知。
- ✨ **魔法重命名**：正则 + 替换式，内置 `$TV` 等魔法关键字与 `{TASKNAME} {SXX} {E} {II} {DATE}`
  等变量；`{II}` 依目标目录现状自动递增编号。
- ⏰ **调度**：APScheduler crontab 全局周期 + 任务级 runweek（星期）/enddate（截止）过滤。
- 📣 **通知**：兼容原项目全部渠道协议（Server酱 / PushDeer / Bark / 钉钉 / 飞书 / Telegram /
  企业微信 / ntfy / 自定义 webhook 等 24 渠道），运行结果树形摘要聚合推送。
- 📥 **下载到本地**：转存成功的新文件可自动落盘——内置流式下载器（.part 断点保护、同大小跳过、
  并发控制、目录镜像/平铺）或投递 **Aria2 RPC**（协议兼容原项目，支持暂停模式）；下载完成后可自动
  触发 **Emby 媒体库刷新**。任务级"下载到本地/递归子目录"开关。
- 🖥 **Web 管理**：任务卡片 + 展开编辑 + 星期胶囊；智能搜索建议（PanSou/CloudSaver 搜资源、
  验证链接、点选建任务）；文件选择器带面包屑与正则处理效果实时预览；SSE 实时运行日志；多账号管理。
- 🧩 **API 与油猴脚本**：Token 鉴权的 `/api/add_task`（兼容旧格式）与 `/api/v1/task/*`，
  附重写好的油猴脚本（`scripts/xiao-pan-auto-save.user.js`）：夸克分享页一键加任务。

## 快速开始（Docker）

```bash
git clone <本仓库> && cd xiao-pan-auto-save
docker compose up -d --build
# 浏览器打开 http://127.0.0.1:8432
```

1. **账号**页粘贴夸克 Cookie（签到需附移动端参数，格式 `cookie#kps=..&sign=..&vcode=..`）；
2. **设置 → 定时规则 / 通知渠道** 按需配置，点"发送测试"验证；
3. **任务**页新建任务（任务名输入几个字即可联网搜资源直接建任务），或"立即运行"看 SSE 日志。

公网部署请务必设置环境变量 `WEBUI_PASSWORD`。

### 下载到本地（可选 Aria2 提速）

内置流式下载器为单连接，夸克**免费账号**常被限速到 ~0.1 MB/s，整集大文件不友好。
需要更快落盘时，用 compose 里可选的 Aria2 服务（多连接突破单流限速）：

```bash
# 1) 连同 aria2 一起启动（aria2 默认不启动，靠 profile 开关）
ARIA2_SECRET=你的RPC密钥 docker compose --profile aria2 up -d --build
```

然后在 **设置 → 下载到本地**：模式选 `aria2`，RPC 地址填 `aria2:6800`，
密钥与上面的 `ARIA2_SECRET` 一致，下载目录用容器内路径 `/app/data/downloads`。

- aria2 与主服务共享同一 `./data:/app/data` 挂载点，应用提交的 `dir` 参数两边解析到同一目录，
  下载结果直接落到宿主机 `./data/downloads`。
- 只想跑主服务时照旧 `docker compose up -d`，aria2 不会被拉起。
- 换 VIP 账号是限速的根治手段；aria2 多连接对免费账号通常也有数倍提升。

## 从 quark-auto-save 迁移

设置 → 旧配置导入：粘贴原 `quark_config.json` 内容，预览确认后一键导入。
映射规则：`cookie(list)`→夸克账号（多个）、`tasklist`→任务
（`update_subdir_resave_mode`→重存模式；插件 `addition` 不支持，会被忽略并列出）、
`crontab / push_config / magic_regex / source`→对应设置项。也可 API 调用：

```bash
curl -X POST http://127.0.0.1:8432/api/migrate -H 'content-type: application/json' \
  --data "{\"config\": $(cat quark_config.json), \"overwrite\": true}"
```

## 本地开发

```bash
py -3.11 -m venv .venv && .venv/Scripts/activate   # Windows；Linux 用 python3 -m venv
pip install -e ".[dev]"
uvicorn backend.main:app --reload --port 8432      # 后端
cd frontend && npm install && npm run dev          # 前端（vite 代理 /api → 8432）
pytest -q && ruff check backend                    # 测试与 lint
```

## 对外 API

- `POST /api/add_task?token=…` —— 兼容原项目油猴脚本格式（`taskname/shareurl/savepath/pattern/replace/ignore_extension/startfid/update_subdir/update_subdir_resave_mode/runweek/enddate`）
- `GET|POST /api/v1/task/list|add|update|run` —— JSON `{success, code, message, data}`
- Token 在 设置 → API 中生成（完整 token 仅创建时展示一次）。

## 相关文档

- [为新网盘写驱动](docs/drivers.md)（含各网盘接入要点与难度评估）
- [从 quark-auto-save 迁移](docs/migrate-from-quark-auto-save.md)

## 声明

本项目仅供学习与个人备份用途，请遵守各网盘服务条款，控制请求频率（默认每天 1-2 次足够）。

## License

AGPL-3.0-only © xiao-pan-auto-save contributors
