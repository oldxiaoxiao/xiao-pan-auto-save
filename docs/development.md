# 开发与发布手册

[项目首页](../README.md) · [驱动开发](drivers.md) · [双版本路线](roadmap.md)

## 项目结构

| 目录/文件 | 职责 |
|---|---|
| `backend/api` | 管理 API、登录、外部 Token、迁移、SSE |
| `backend/core` | 追更引擎、调度、命名、日志与下载注册表 |
| `backend/drivers` | 网盘协议、能力声明及自动注册 |
| `backend/services` | 账号、任务、搜索、通知、下载和历史服务 |
| `backend/tests` | 隔离数据库测试、模拟 HTTP 与可选 Aria2 真机测试 |
| `frontend/src` | Vue 页面、组件、API、状态与样式 |
| `desktop` | 原生窗口、本机服务、单实例及数据路径 |
| `scripts` | CLI、备份、油猴、桌面及 Compose 打包 |
| `.github/workflows` | 测试、多平台编译、镜像和 Release 发布 |

## 环境准备

Python 3.11+、Node.js 22+；Dockerfile 和 CI 使用 Node.js 24。普通后端开发不需要 GUI 打包依赖。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cd frontend
npm ci
```

Windows 使用 `py -3.11 -m venv .venv` 和 `.venv\Scripts\Activate.ps1` 激活环境。

两个终端分别运行：

```bash
# 仓库根目录
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8432
```

```bash
# frontend 目录
npm run dev
```

访问 Vite 显示的开发地址，`/api` 代理到 8432 并保留浏览器 Host，以兼容同源写请求保护。后端生产模式会托管 `frontend/dist`，重新编译后不缓存首页。

## 常规验证

```bash
python -m pytest backend/tests -q
ruff check backend desktop scripts/build_desktop.py scripts/build_compose_release.py
cd frontend
npm run typecheck
npm run lint:check
npm run build
```

测试通过 `conftest.py` 在导入后端前设置临时 `DATA_DIR`，避免清理真实账号、任务和下载历史。添加测试时保持该隔离，不在用例中接入真实 Cookie 或私人通知服务。

`lint:check` 中的既有 Vue 布局规则可能输出 warning；warning 与 error 需分别记录。构建的主包体积告警是后续优化项，不能用提高阈值掩盖。

## Aria2 真机测试

普通套件默认跳过 `test_download_aria2_live.py`。该文件头部给出独立容器、端口 6801、共享临时下载目录和 `P3TERX` 测试密钥的准备方式，使用时先核对本地端口和容器名没有冲突。

```bash
XIAO_PAN_ARIA2_E2E=1 python -m pytest backend/tests/test_download_aria2_live.py -q
```

测试验证真实 Aria2 的投递、落盘、结果查询、重下、停止和 Emby HTTP 接线，使用本地生成文件和临时数据库。它不是网盘账号或真实 Emby 媒体库的端到端测试。用完只回收自己创建的测试容器。

## 桌面开发与编译

```bash
python -m pip install '.[desktop]'
python -m desktop.main
python scripts/build_desktop.py
```

桌面脚本会启动本机子进程，设置用户目录、随机密码及一次性凭证，等服务就绪后打开窗口。`--serve` 是内部参数，不是公开服务启动方式。

打包使用 `desktop.spec`，包含前端资源、动态网盘驱动、时区数据和 GUI 依赖。打包过程中数据目录指向 `build/packaging-data`，冻结服务冒烟用临时目录，制品不会包含真实 `data/`。Mac 使用 `ditto` 归档保留应用框架的符号链接。

可以在前端已经重新构建后使用 `python scripts/build_desktop.py --skip-frontend`。不要用旧 dist 打包新代码。`dist/` 是冻结应用，`release/` 是 ZIP 和校验和，均被 Git 忽略。

Windows x64、macOS arm64 和 macOS x64 在各自原生 runner 编译；PyInstaller 不用于跨平台生成二进制。当前没有发行者签名，后续签名密钥应放 CI secrets，不提交到代码库。[GitHub runner 架构](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[PyInstaller 构建选项](https://pyinstaller.org/en/stable/usage.html)。

## 发布流程

1. 同步 `backend/__init__.py`、`frontend/package.json`、`frontend/package-lock.json`、macOS bundle 版本和 Compose 默认标签。
2. 更新 `docs/releases.md`、当前 release notes、验收记录和平台限制。
3. 完整测试、前端编译、容器配置/启动与客户端冒烟通过后提交。
4. 推送发行标签，例如 `v0.1.1`。`Build and release` 工作流先验证，再生成三个桌面包和双架构镜像。
5. 所有构建成功后，工作流创建公开 Release，上传桌面 ZIP、Compose ZIP 和 `SHA256SUMS`。失败时不会把缺失的平台伪装成已交付。
6. 核对 Release 的 commit、附件、校验和，以及 GHCR 镜像的平台列表；按版本标签部署验证。

源码分支推送到 `codex/release-*` 或手动运行工作流只编译并保存 Actions artifacts，不发布公开 Release。发行标签不得复用已有版本；失败后按日志修复再发布，不能绕过验证门槛。

Compose 附件生成需要 `pyyaml`：`python -m pip install pyyaml && python scripts/build_compose_release.py`。这只是打包依赖，不是运行服务依赖。

## 扩展约定

新网盘实现最小五原语和错误语义后才声明 `supported=True`；可选能力必须在能力集合中声明。参考[驱动手册](drivers.md)。新增功能同时核对 Web、Compose 和桌面的数据/下载语义；与系统有关的功能放桌面层，业务规则放共享服务。
