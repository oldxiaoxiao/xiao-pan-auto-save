/** 与后端 API 契约一一对齐的类型定义。 */

/** GET/POST/PUT /api/tasks 的任务字段（不含 id 的请求体使用 TaskPayload）。 */
export interface TaskPayload {
  taskname: string;
  shareurl: string;
  savepath: string;
  pattern: string;
  replace: string;
  ignore_extension: boolean;
  startfid: string;
  update_subdir: string;
  update_subdir_resave: boolean;
  enddate: string;
  runweek: number[];
  auto_download: boolean;
  download_subdir: boolean;
  download_savepath: string;
  disabled: boolean;
  account_id: number | null;
  sort_order: number;
  episode_start: number;
  episode_end: number;
  quality: string;
  schedule: string;
}

export interface Task extends TaskPayload {
  id: number;
  shareurl_ban: string;
  last_run_at: string | null;
}

/** GET /api/accounts 返回（无 cookie 明文，仅掩码）。 */
export interface Account {
  id: number;
  name: string;
  driver_key: string;
  enabled: boolean;
  sort_order: number;
  nickname: string;
  member_type: string;
  capacity_used: number;
  capacity_total: number;
  can_save: boolean;
  last_check_at: string | null;
  last_sign_at: string | null;
  sign_message: string;
  cookie_masked: string;
}

/** POST/PUT /api/accounts 请求体。 */
export interface AccountPayload {
  name: string;
  driver_key: string;
  cookie: string;
  enabled: boolean;
  sort_order: number;
}

/** POST /api/accounts/refresh 与 /sign 的每项结果。 */
export interface AccountActionResult {
  id: number;
  ok: boolean;
  nickname?: string;
  message: string;
  reward?: string;
}

/** GET /api/settings 汇总。 */
export interface PushConfig {
  [key: string]: unknown;
}

export interface MagicRegexRule {
  pattern: string;
  replace?: string;
}

export interface MagicRegex {
  [keyword: string]: MagicRegexRule;
}

export interface PansouSource {
  server?: string;
  enable?: boolean | string;
}

export interface CloudSaverSource {
  server?: string;
  username?: string;
  password?: string;
  token?: string;
  enable?: boolean | string;
}

export interface SearchSource {
  pansou?: PansouSource;
  cloudsaver?: CloudSaverSource;
  [key: string]: unknown;
}

export interface DownloadSettings {
  mode: "builtin" | "aria2";
  dir: string;
  concurrency: number;
  aria2: { host_port: string; secret: string; pause: boolean };
  emby: { url: string; token: string };
}

export interface Settings {
  crontab: string;
  push_config: PushConfig;
  magic_regex: MagicRegex;
  source: SearchSource;
  notify_enabled: boolean;
  sign_enabled: boolean;
  download: DownloadSettings;
}

/** PUT /api/settings/{key} 可写的键集合。 */
export type SettingKey = keyof Settings;

export interface NotifyTestResult {
  channel: string;
  ok: boolean;
  message: string;
}

export interface NotifyTestResponse {
  enabled: string[];
  results: NotifyTestResult[];
}

export interface DriverInfo {
  key: string;
  name: string;
  supported: boolean;
  capability: string[];
  share_domains: string[];
}

export interface SchedulerInfo {
  next_run: string | null;
  trigger: string | null;
}

/** 文件浏览 / 分享预览共用的条目。 */
export interface FsItem {
  fid: string;
  name: string;
  is_dir: boolean;
  size: number;
  mtime: string;
  token: string;
  /** 分享预览：正则处理后的名字（未匹配 pattern 时无此字段）。 */
  name_re?: string;
  /** 分享预览：目标目录已存在时的现有名。 */
  saved_as?: string;
}

export interface DirList {
  path: string;
  parent: string;
  at_root: boolean;
  list: FsItem[];
}

export interface SharePreview {
  ok: boolean;
  banned?: boolean;
  message?: string;
  path: string;
  sub_names?: string[];
  list: FsItem[];
}

export interface SharePreviewPayload {
  shareurl: string;
  path: string;
  taskname: string;
  pattern: string;
  replace: string;
  ignore_extension: boolean;
  update_subdir: string;
  savepath: string;
}

export interface LogEntry {
  ts: string;
  level: string;
  message: string;
  run_id: string;
  task_id: number | null;
}

/** POST /api/tasks/run 的 summary 行 message 反序列化结构。 */
export interface RunSummary {
  run_id: string;
  trigger: string;
  total: number;
  updated: number;
  skipped: number;
  failed: number;
  notify_lines: number;
  error?: string;
}

export interface Suggestion {
  shareurl: string;
  taskname: string;
  content: string;
  datetime: string;
  channel: string;
  source: string;
}

export interface SuggestionResponse {
  ok: boolean;
  data: Suggestion[];
}

export interface ValidateResponse {
  ok: boolean;
  count?: number;
  message?: string;
  pending?: boolean;
  retry?: boolean;
}

export interface HealthResponse {
  status: string;
  data_dir: string;
}

/** GET /api/downloads 的下载任务项（内置下载队列）。 */
export interface DownloadJob {
  id: string;
  task_id: number | null;
  taskname: string;
  filename: string;
  dest_path: string;
  total: number;
  done: number;
  speed: number;
  status: "queued" | "downloading" | "paused" | "stopped" | "done" | "failed" | "skipped";
  source: "builtin" | "aria2";
  error: string;
  started_at: number;
  updated_at: number;
}

export type FileState = "ok" | "missing" | "unknown";

/** GET /api/downloads/history 的账本行（download_record 表）。 */
export interface DownloadRecord {
  id: number;
  source: "builtin" | "aria2";
  ref_id: string;
  task_id: number | null;
  taskname: string;
  filename: string;
  dest_path: string;
  size_total: number;
  size_done: number;
  fid: string;
  driver_key: string;
  account_id: number | null;
  status: "queued" | "downloading" | "done" | "failed" | "skipped" | "stopped";
  error: string;
  created_at: string;
  finished_at: string | null;
  /** 磁盘到位校验，由历史查询接口补出。 */
  file_state?: FileState;
}

/** GET /api/downloads/history 的查询参数。 */
export interface DownloadHistoryQuery {
  page?: number;
  page_size?: number;
  status?: string;
  task_id?: number | null;
  keyword?: string;
}
