小盘自动转存 0.1.1：共享一套业务引擎，提供 Docker Compose / Web、Windows x64、macOS Apple Silicon 和 Intel 客户端。

### 本次变化

- 原生桌面窗口，安装包内置 Python 后端，使用系统用户数据目录和单实例保护。
- 修复 Web 管理密码未生效、静态文件越界、默认下载目录及 Windows 文件名兼容问题。
- 修复迁移部分提交、误删其他账号和导入后继续沿用旧定时规则的问题。
- Compose 默认绑定本机，提供 Linux amd64/arm64 镜像与独立配置包。
- 重写项目首页，功能、配置、升级、开发和客户端路线分别独立成文。

### 下载选择

| 附件 | 平台 |
|---|---|
| `*-compose.zip` | Docker Compose，镜像 `ghcr.io/oldxiaoxiao/xiao-pan-auto-save:0.1.1` |
| `*-windows-x64.zip` | Windows x64；完整解压后打开 `XiaoPan.exe` |
| `*-macos-arm64.zip` | Apple Silicon Mac；解压 `XiaoPan.app` |
| `*-macos-x64.zip` | Intel Mac；解压 `XiaoPan.app` |
| `SHA256SUMS` | ZIP 附件校验和 |

### 使用和升级

当前只有夸克驱动可用，其他网盘仍是骨架。首次使用先验证 Cookie，再试跑并手动运行一个任务。

Compose 备份 `data/` 后升级；局域网访问需配置 `WEBUI_BIND=0.0.0.0` 和密码。桌面版替换程序时保留用户数据目录。网页新建任务默认开启下载，可在新建默认中关闭。

客户端退出或电脑休眠时停止追更；0.1.1 无托盘、自启、自动更新或远程连接模式。客户端未进行发行者签名和 Apple 公证，Windows 需要系统 WebView2 Runtime。未使用真实网盘 Cookie 或私人通知凭证进行本次端到端验证；外部集成需在用户环境确认。

详细说明见仓库的 README、`docs/releases.md`、`docs/desktop.md` 和 `docs/verification-0.1.1.md`。所有附件发布前经过平台原生构建及冻结后服务冒烟验证，GUI 验证范围以验收记录为准。
