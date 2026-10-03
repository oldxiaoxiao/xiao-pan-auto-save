# 从 quark-auto-save 迁移配置

## 方式一：Web 界面（推荐）

1. 打开原项目 WebUI（或找到原项目目录下的 `config/quark_config.json`），全选复制 JSON 内容；
2. 本系统 → **设置 → 旧配置导入**，粘贴 → **预览**（显示将导入的账号数 / 任务数 / 设置项，
   以及因插件 `addition` 不兼容而被忽略的任务清单）；
3. 若本系统已有数据，勾选"覆盖导入"（会清空现有任务与夸克账号，操作不可撤销）后确认导入。

## 方式二：API

```bash
# 预览
curl -X POST http://127.0.0.1:8432/api/migrate/preview \
  -H 'content-type: application/json' \
  -d "{\"config\": $(cat config/quark_config.json | tr -d '\n')}"

# 导入（overwrite 可选）
curl -X POST http://127.0.0.1:8432/api/migrate \
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
| `source` | 设置 → 资源搜索源 | pansou/cloudsaver |
| `tasklist[].taskname/shareurl/savepath/pattern/replace` | 同名字段 | |
| `tasklist[].ignore_extension/startfid/update_subdir/enddate` | 同名字段 | |
| `tasklist[].update_subdir_resave_mode` | `子目录重存模式` | |
| `tasklist[].runweek/enddate/禁用状态/shareurl_ban` | 同语义字段 | |
| `tasklist[].addition`（插件配置） | **不迁移** | 插件系统未在本期范围，导入报告中列出受影响任务 |
| `plugins` | 不迁移 | 同上 |
| `webui` | 环境变量 | `WEBUI_PASSWORD` 等 |

## 迁移后检查清单

- [ ] 账号页对新账号点"健康检查"，确认昵称/Cookie 有效；
- [ ] 有移动端签到的账号 Cookie 尾部应含 `#kps=..&sign=..&vcode=..`，点"全部签到"验证；
- [ ] 设置 → 通知渠道 点"发送测试"；
- [ ] 任务页先对单个任务点"立即运行"，在 SSE 日志确认差集与重命名符合预期；
- [ ] 原项目请**停止其定时任务**，避免双端重复转存。
