# 从 quark-auto-save 迁移配置

[项目首页](../README.md) · [配置参考](configuration.md)

先停止旧项目的定时任务，并备份本系统的数据目录。0.1.1 的导入使用单一数据库事务，失败不会保留半份数据；成功后立即按导入配置重建调度。

## 方式一：Web 界面（推荐）

1. 打开原项目 WebUI（或找到原项目目录下的 `config/quark_config.json`），全选复制 JSON 内容；
2. 本系统 → **设置 → 旧配置导入**，粘贴 → **预览**（显示将导入的账号数 / 任务数 / 设置项，
   以及因插件 `addition` 不兼容而被忽略的任务清单）；
3. 若本系统已有数据，勾选"覆盖导入"（会清空现有任务与夸克账号，操作不可撤销）后确认导入。

## 方式二：API

```bash
# 预览
curl -u admin -X POST http://127.0.0.1:8432/api/migrate/preview \
  -H 'content-type: application/json' \
  -d "{\"config\": $(cat config/quark_config.json | tr -d '\n')}"

# 导入（overwrite 可选）
curl -u admin -X POST http://127.0.0.1:8432/api/migrate \
  -H 'content-type: application/json' \
  -d "{\"config\": $(cat config/quark_config.json | tr -d '\n'), \"overwrite\": true}"
```

## 字段映射表

| quark-auto-save | xiao-pan-auto-save | 说明 |
|---|---|---|
| `cookie`（str 或 list） | 账号页：夸克账号 1..N | list 全部导入，第一个为默认转存账号 |
| `crontab` | 设置 → 定时规则 | |
| `push_config` | 设置 → 通知渠道 | 键名协议完全兼容 |
| `magic_regex` | 设置 → 魔法匹配 | |
| `source` | 设置 → 资源搜索源 | 旧结构归一化为多引擎配置 |
| `tasklist[].taskname/shareurl/savepath/pattern/replace` | 同名字段 | |
| `tasklist[].ignore_extension/startfid/update_subdir/enddate` | 同名字段 | |
| `tasklist[].update_subdir_resave_mode` | `子目录重存模式` | |
| `tasklist[].runweek/enddate/禁用状态/shareurl_ban` | 同语义字段 | |
| `tasklist[].addition.aria2` | 下载字段 | 迁移 auto_download、download_subdir、save_path |
| `tasklist[].addition` 的其他插件 | 不迁移 | 导入报告列出携带 addition 的任务 |
| `plugins.aria2` | 设置 → 下载设置 | 迁移 RPC 地址、密钥、目录、暂停选择 |
| `plugins` 的其他插件 | 不迁移 | 不执行旧插件代码 |
| `webui` | 环境变量 | `WEBUI_PASSWORD` 等 |

## 迁移后检查清单

- [ ] 账号页对新账号点"健康检查"，确认昵称/Cookie 有效；
- [ ] 有移动端签到的账号 Cookie 尾部应含 `#kps=..&sign=..&vcode=..`，点"全部签到"验证；
- [ ] 设置 → 通知渠道 点"发送测试"；
- [ ] 任务页先对单个任务点"立即运行"，在 SSE 日志确认差集与重命名符合预期；
- [ ] 原项目请**停止其定时任务**，避免双端重复转存。

覆盖导入会删除全部旧任务与夸克账号，保留非夸克账号和下载历史。旧下载历史仍指向原路径/账号；跨机器迁移需重新核对文件路径。缺少任务名、链接或保存目录的旧任务会跳过，预览任务数可能大于实际导入数。导入新的 Cookie 不代表该 Cookie 已通过网盘验证。
