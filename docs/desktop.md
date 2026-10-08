# Windows / macOS 客户端

[返回项目首页](../README.md) · [功能手册](features.md) · [版本差异与路线](roadmap.md)

桌面客户端将现有 Vue 管理界面和 Python 后端一起打包。使用者不需要安装 Python、Node.js 或 Docker。客户端实际运行的仍是同一套追更、转存、下载和 SQLite 服务。

## 下载与启动

从 [GitHub Releases](https://github.com/oldxiaoxiao/xiao-pan-auto-save/releases) 下载匹配平台的 0.1.1 附件：

| 平台 | 制品 | 启动方式 |
|---|---|---|
| Windows x64 | `xiao-pan-auto-save-0.1.1-windows-x64.zip` | 完整解压文件夹，双击 `XiaoPan/XiaoPan.exe` |
| macOS Apple Silicon | `xiao-pan-auto-save-0.1.1-macos-arm64.zip` | 解压后将 `XiaoPan.app` 拖入应用程序，双击打开 |
| macOS Intel | `xiao-pan-auto-save-0.1.1-macos-x64.zip` | 同上 |

Windows 依赖系统 WebView2 Runtime；缺少时安装 [Microsoft 官方运行时](https://developer.microsoft.com/microsoft-edge/webview2/)。Windows ZIP 中的 `_internal` 目录必须与 exe 一起保留。

0.1.1 的客户端没有发行者代码签名和 Apple 公证。系统可能提示未知开发者；确认下载来源和 SHA256 后，使用系统提供的允许打开入口。此版本不承诺覆盖所有旧系统；原生构建平台和已执行验证见[验收记录](verification-0.1.1.md)。

## 首次使用

1. 打开客户端，在账号页添加夸克 Cookie，并检查账号是否有效。
2. 在设置页确认新建任务默认值、下载目录、搜索源和通知渠道。
3. 新建任务填写名称、分享链接、网盘保存目录，先使用“试跑”检查筛选结果。
4. 保存后手动运行一次，在日志及下载页确认结果，再启用追更。

## 数据与下载

| 平台 | 默认数据目录 |
|---|---|
| Windows | `%LOCALAPPDATA%\XiaoPan` |
| macOS | `~/Library/Application Support/XiaoPan` |

目录包含 `xiao_pan.db`、`logs/`、`backups/`、`downloads/`。内置下载默认写入其中的 `downloads/`，也可在设置页填写其他绝对路径。升级只替换程序，不覆盖这些数据。数据库包含账号凭证，备份和迁移时按私人文件保管。

开发或测试可用 `XIAO_PAN_DESKTOP_DATA_DIR` 指定独立数据目录。桌面数据与源码目录下的 `data/` 默认分开；如需迁移，在两端停止后复制数据库，并重新确认下载目录。直接复制数据库不会搬运下载文件，也不会改写历史记录中的路径。

## 运行与退出

- 同一个数据目录只允许启动一个客户端实例。
- 客户端服务使用随机端口，仅监听 `127.0.0.1`，每次启动使用随机密码和一次性凭证，窗口自动登录。
- 关闭窗口会提示退出，并停止本地服务、调度和当前下载。内置下载可能留下 `.part`；下次重新下载会重取完整文件，并非跨重启断点续传。
- 电脑关机、休眠或客户端退出时不会执行追更。需要全天运行时使用 Compose 版。
- 0.1.1 没有托盘驻留、开机自启、自动更新、桌面通知或远程服务器切换。后续按[路线](roadmap.md)推进。

默认使用内置下载器。客户端可以连接已有 Aria2 RPC，但不自带 Aria2；远程 Aria2 的下载路径和客户端可访问路径需自行对齐。暂停/继续只适用于 Aria2，内置下载支持停止。

## 故障排查

- 打不开窗口：检查系统 WebView 依赖，确认 ZIP 已完整解压、平台和架构匹配。
- 服务启动失败：查看数据目录 `logs/desktop-startup.log` 和 `logs/desktop-server.log`。
- 提示已经运行：切换到现有窗口。实例锁由操作系统释放，无需手动删除锁文件。
- 文件下载失败：检查账号有效性、直链日志、目标盘权限和剩余容量；Windows 设备保留名会自动加下划线。

## 开发构建

在对应平台安装 Python 3.11+ 和 Node.js 22+：

```bash
python -m pip install '.[desktop]'
python scripts/build_desktop.py
```

脚本编译前端、冻结应用、用临时数据启动冻结后的服务验证，再生成 ZIP 和 SHA256。构建输出位于 `dist/` 和 `release/`。Windows、macOS 的包需分别在原生平台编译。

实现依据：[pywebview 打包指南](https://pywebview.flowrl.com/guide/freezing.html)、[PyInstaller 平台构建与签名](https://pyinstaller.org/en/stable/usage.html)。
