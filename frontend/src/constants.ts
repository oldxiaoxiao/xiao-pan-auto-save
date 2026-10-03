import type { SettingKey } from "./api/types";

/** 常用通知渠道：与后端 notify_service.CHANNELS 的键位对齐，供设置页 KV 编辑器渲染。 */
export interface ChannelSpec {
  name: string;
  label: string;
  keys: string[];
  bool?: boolean;
}

export const NOTIFY_CHANNELS: ChannelSpec[] = [
  { name: "CONSOLE", label: "控制台输出", keys: [], bool: true },
  { name: "BARK", label: "Bark (iOS)", keys: ["BARK_PUSH"] },
  { name: "SERVERCHAN", label: "Server 酱", keys: ["PUSH_KEY"] },
  { name: "PUSH_PLUS", label: "PushPlus", keys: ["PUSH_PLUS_TOKEN"] },
  { name: "TG_BOT", label: "Telegram", keys: ["TG_BOT_TOKEN", "TG_USER_ID"] },
  { name: "DD_BOT", label: "钉钉机器人", keys: ["DD_BOT_TOKEN", "DD_BOT_SECRET"] },
  { name: "FEISHU", label: "飞书机器人", keys: ["FSKEY"] },
  { name: "QYWX_KEY", label: "企业微信机器人", keys: ["QYWX_KEY"] },
  { name: "QYWX_AM", label: "企业微信应用", keys: ["QYWX_AM"] },
  { name: "IGOT", label: "iGot", keys: ["IGOT_PUSH_KEY"] },
  { name: "PUSHDEER", label: "PushDeer", keys: ["DEER_KEY"] },
  { name: "NTFY", label: "ntfy", keys: ["NTFY_TOPIC"] },
  { name: "WEBHOOK", label: "自定义 Webhook", keys: ["WEBHOOK_URL", "WEBHOOK_METHOD"] },
];

/** 设置页可写键（与后端 EDITABLE_KEYS 对齐）。 */
export const EDITABLE_KEYS: SettingKey[] = [
  "crontab",
  "push_config",
  "magic_regex",
  "source",
  "notify_enabled",
  "sign_enabled",
  "download",
];

/** 常用 crontab 快捷预设。 */
export const CRON_PRESETS: { label: string; value: string }[] = [
  { label: "每天 9:00", value: "0 9 * * *" },
  { label: "每天 7:30", value: "30 7 * * *" },
  { label: "每 6 小时", value: "0 */6 * * *" },
  { label: "每小时", value: "0 * * * *" },
  { label: "每天 0:00", value: "0 0 * * *" },
];

/** 任务正则可插入的魔法变量。 */
export const MAGIC_VARIABLES = ["{TASKNAME}", "{E}", "{II}", "{EXT}", "{DATE}", "{S}", "{SXX}"];
