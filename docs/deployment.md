# Docker Compose 部署与升级

[项目首页](../README.md) · [配置参考](configuration.md) · [版本日志](releases.md)

## 使用发行镜像

从 [Releases](https://github.com/oldxiaoxiao/xiao-pan-auto-save/releases) 下载 `xiao-pan-auto-save-0.1.1-compose.zip`，解压到固定目录后执行：

```bash
cp .env.example .env
# 按需编辑 .env，尤其是访问密码和监听地址
docker compose pull
docker compose up -d
```

浏览器打开 `http://127.0.0.1:8432`。首次运行先添加夸克 Cookie，再配置新建默认和下载目录，最后创建任务并试跑。

镜像地址为 `ghcr.io/oldxiaoxiao/xiao-pan-auto-save:0.1.1`，发布工作流生成 Linux amd64 / arm64 镜像。版本化标签便于固定运行环境，日常升级建议明确修改版本，不依赖 `latest`。

默认仅绑定 `127.0.0.1`。需要局域网访问时，在 `.env` 设置 `WEBUI_BIND=0.0.0.0` 和非空 `WEBUI_PASSWORD`，重建服务；Web 用户名默认为 `admin`。公网接入应通过 HTTPS 反向代理。管理界面密码与对外 API Token 是两套独立鉴权。

## 从源码构建

```bash
git clone https://github.com/oldxiaoxiao/xiao-pan-auto-save.git
cd xiao-pan-auto-save
git checkout v0.1.1
cp .env.example .env
docker compose up -d --build
```

源码仓库的 Compose 包含 `build: .`；发行 ZIP 的 Compose 只引用镜像，不要求安装构建工具。Dockerfile 分两阶段编译前端与安装 Python 服务，构建上下文排除用户数据和开发环境。

## 数据与下载路径

`./data:/app/data` 保存 SQLite 数据库、运行日志、数据库备份和默认下载文件。浏览器里的网盘保存路径属于云端，下载根目录属于运行后端的机器。

| 用途 | 容器路径 | 宿主机路径 |
|---|---|---|
| 数据库 | `/app/data/xiao_pan.db` | `./data/xiao_pan.db` |
| 日志 | `/app/data/logs` | `./data/logs` |
| 备份 | `/app/data/backups` | `./data/backups` |
| 默认下载 | `/app/data/downloads` | `./data/downloads` |

自定义下载目录须挂载进容器，并在设置页填写容器内的绝对路径。直接填宿主机路径不会自动创建对应挂载。

## 可选 Aria2

在 `.env` 中设置 `ARIA2_SECRET`，然后：

```bash
docker compose --profile aria2 up -d
```

设置 → 下载设置：模式选 `aria2`，RPC 地址 `aria2:6800`，密钥与 `.env` 一致，下载目录 `/app/data/downloads`。主服务和 Aria2 挂载到相同路径，因此历史页能检查实际文件。Aria2 RPC 默认不对宿主机开放。

Aria2 使用第三方 `p3terx/aria2-pro` 镜像，Compose 直接指定 `aria2c` 参数；这里的密钥来自 `--rpc-secret`，不是该镜像入口脚本的默认密钥。多连接不保证绕过网盘的账号限速。RPC 不可达时，现有下载流程可降级到内置下载器；不要把降级理解为任何下载错误都能自动恢复。

若后端在宿主机运行、Aria2 在容器运行，必须把下载目录挂载到双方相同的绝对路径，并将 RPC 地址改为宿主机可访问的地址，例如 `127.0.0.1:6800`。容器里的 `aria2:6800` 服务名不能直接用在宿主机后端。

## 状态与日志

```bash
docker compose ps
docker compose logs --tail=100 xiao-pan
curl http://127.0.0.1:8432/api/health
```

健康检查只证明服务能响应，不证明账号或第三方服务可用。账号页检查、任务试跑和一次真实运行分别验证这些环节。

## 备份、升级与回退

1. 停止服务后备份整个 `data/` 和 `.env`，包含下载文件时注意容量。
2. 修改 `.env` 的 `XIAO_PAN_VERSION`，执行 `docker compose pull && docker compose up -d`。
3. 检查服务健康、账号、调度时间和一个任务试跑。

源码部署升级时切换对应标签并重新 `--build`。应用启动会先备份已有数据库，再补齐新增列；这是启动备份，不是定时完整备份。`scripts/backup_db.sh` 可按需配置到宿主机定时任务。

回退时同时恢复升级前数据库和匹配的程序版本；仅改镜像标签不能保证新数据库结构与旧程序完全兼容。停止/升级后正在运行的内置下载会中断，历史记录及已完成文件仍保留。

## Compose 版的运行边界

按单个后端进程设计，不要启动多个 Uvicorn worker 或多个容器同时访问同一个数据库、驱动同一批任务。数据库和下载目录是私人数据，不随发行包提供；迁移到客户端时需要停止原服务并重新确认文件路径。
