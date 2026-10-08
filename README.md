# 小盘自动转存 · xiao-pan-auto-save

<p align="center">
  <img src="assets/logo.png" width="160" alt="小盘自动转存" />
</p>

**把网盘分享链接变成可管理的追更任务。** 小盘自动转存会按计划检查分享内容，将新增文件转存到你的网盘，按规则整理命名，并按需下载到本地、推送执行结果。

提供 **Docker Compose / Web、Windows 客户端和 macOS 客户端**三种交付方式，复用同一套业务引擎。当前版本为 **0.1.1**，当前已实现的网盘驱动为 **夸克**。

[下载发行版](https://github.com/oldxiaoxiao/xiao-pan-auto-save/releases) · [部署指南](docs/deployment.md) · [客户端使用](docs/desktop.md) · [版本日志](docs/releases.md)

## 能做什么

- **自动转存与追更**：只转存新增内容，支持集数、画质、扩展名和子目录筛选，发现失效分享时标记并通知。
- **灵活执行**：定时追更、仅手动、一次性三种方式；全局与任务独立调度，失败重试、截止日期和星期限制。
- **整理文件**：正则重命名、魔法变量和连续编号，支持网盘目录浏览及保存前只读试跑。
- **搜索并创建任务**：聚合 PanSou、夸克搜和自建 CloudSaver，支持多实例、单源切换和重复链接合并。
- **下载与记录**：内置下载器或 Aria2 RPC，查看实时进度、下载历史、文件到位状态，并对失败文件重新下载。
- **账号与通知**：夸克多账号、Cookie 掩码、健康检查与签到；多渠道结果推送，支持 Emby 刷新请求。
- **接入现有工作流**：带鉴权的管理界面、独立 Token API、夸克分享页油猴脚本及旧配置迁移。

具体行为、默认值和限制见[功能手册](docs/features.md)，已执行的验证见[0.1.1 验收记录](docs/verification-0.1.1.md)。

## 界面预览

| 任务与试跑 | 下载进度与历史 |
|---|---|
| ![任务管理](images/1.png) | ![下载管理](images/2.png) |
| **实时日志** | **账号管理** |
| ![运行日志](images/3.png) | ![网盘账号](images/4.png) |

截图展示主要功能；不同版本的界面细节可能变化。

## 选择使用方式

| 版本 | 适合场景 | 使用入口 |
|---|---|---|
| Docker Compose / Web | NAS、服务器、长期运行和浏览器访问 | [部署指南](docs/deployment.md) |
| Windows 客户端 | 个人电脑，双击运行，本地下载 | [客户端指南](docs/desktop.md) |
| macOS 客户端 | Intel / Apple Silicon 电脑 | [客户端指南](docs/desktop.md) |

客户端退出、电脑关机或休眠后会停止追更；需要全天运行时使用 Compose。两个版本的数据目录和运行方式不同，完整对比与后续安排见[版本路线](docs/roadmap.md)。

## 支持范围

**夸克**驱动已实现转存、目录管理、重命名、下载、账号检查与签到。迅雷、123、百度、阿里、115、天翼、移动和 AList 目前仅有驱动骨架，尚不能作为可用网盘使用。

公开搜索站、网盘接口和通知服务属于外部依赖，其连通性、限速及账号权限可能变化。首次使用请检查账号，先试跑一个任务，再确认真实转存与下载结果。

## 文档导航

| 文档 | 内容 |
|---|---|
| [功能手册](docs/features.md) | 任务、追更、筛选、下载、日志、API 与已知限制 |
| [部署指南](docs/deployment.md) | Compose 安装、Aria2、数据备份与升级 |
| [客户端指南](docs/desktop.md) | Windows/macOS 启动、数据目录与运行边界 |
| [配置参考](docs/configuration.md) | 环境变量、账号、调度、通知、搜索及下载设置 |
| [升级日志](docs/releases.md) | 各版本变化与升级注意事项 |
| [开发手册](docs/development.md) | 项目结构、开发环境、测试、打包与发布 |
| [双版本路线](docs/roadmap.md) | 共用架构、版本差异、后续待做事项与验收标准 |
| [驱动开发](docs/drivers.md) | 网盘驱动接口与扩展约定 |
| [旧配置迁移](docs/migrate-from-quark-auto-save.md) | quark-auto-save 的导入步骤和字段映射 |
| [0.1.1 验收记录](docs/verification-0.1.1.md) | 测试、构建、平台验证及尚未验证的集成 |

## 项目与许可

采用 **FastAPI + SQLite + Vue 3**，网盘操作通过可插拔驱动接入。业务思路源于 [quark-auto-save](https://github.com/Cp0204/quark-auto-save)，本项目重新组织了架构和管理界面。

使用本项目进行个人备份时，请遵守网盘服务条款并设置合理请求频率。项目以 [AGPL-3.0-only](LICENSE) 发布。
