/** 与后端 API 契约一一对齐的类型定义。 */

/** 执行方式：follow=定时追更（受定时器驱动）| manual=仅手动 | once=一次性（拿到新增且下载全成功后自动停用）。 */
export type RunMode = "follow" | "manual" | "once";

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
  run_mode: RunMode;
}

export interface Task extends TaskPayload {
  id: number;
  shareurl_ban: string;
  last_run_at: string | null;
  /** 后端 _to_out 已按 run_mode_of 归一化，响应里只会是 follow / manual / once 三值之一。 */
  run_mode: RunMode;
  /** 一次性任务本轮已消耗的真失败重试次数（成功或手动「▶ 运行」归零；非 once 行恒为 0）。 */
  retry_attempts: number;
  /** 到点重试的时间，后端 `isoformat()` 出的无时区串，与 last_run_at 同一口径；null=没有待重试。 */
  next_retry_at: string | null;
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
  history_retention: "days_30" | "days_90" | "days_180" | "forever";
  aria2: { host_port: string; secret: string; pause: boolean };
  emby: { url: string; token: string };
}

/** GET/PUT /api/settings 的 task_defaults：只影响新建任务，绝不回写已有任务。 */
export interface TaskDefaults {
  savepath_root: string;
  auto_download: boolean;
  run_mode: RunMode;
  pattern: string;
  quality: string;
  subdir_filter: boolean;
}

/** POST /api/tasks/dry-run 的响应（只读试跑；判定与真实运行同源）。 */
export interface DryRunResult {
  ok: boolean;
  status: string;
  message: string;
  new_count?: number;
  total_size?: number;
  skipped_existing?: number;
  filtered_out?: number;
  items?: { share_name: string; final_name: string; dest_path: string; is_dir: boolean }[];
}

/** GET /api/settings/magic/expand 的响应。 */
export interface MagicExpand {
  ok: boolean;
  name: string;
  pattern: string;
  replace: string;
}

export interface Settings {
  crontab: string;
  push_config: PushConfig;
  magic_regex: MagicRegex;
  source: SearchSource;
  notify_enabled: boolean;
  sign_enabled: boolean;
  download: DownloadSettings;
  task_defaults: TaskDefaults;
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
  /** 本次载入的行数，**含停用行**（这一批处理了几个看 driven）。 */
  total: number;
  /** 读作「本次处理」的行数 = total - skipped - disabled_skipped；后端派生，不单独计数。
   *  含「链接没有支持的驱动」「没有可用账号」这两类只加 failed 就跳过的行——它们被处理过但没进转存引擎。 */
  driven: number;
  updated: number;
  skipped: number;
  /** 批量「立即运行」跳过的已停用任务数（行内「▶ 运行」不计）。 */
  disabled_skipped: number;
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

/** 到位校验：ok=在；missing=已丢失；partial=常规文件在但大小与账本不符；unknown=未校验（含在途行）。 */
export type FileState = "ok" | "missing" | "partial" | "unknown";

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
