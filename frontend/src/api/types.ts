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
  /** FR-06：指定账号不可用时是否允许切到同网盘的其它可用账号。 */
  account_failover: boolean;
  sort_order: number;
  episode_start: number;
  episode_end: number;
  quality: string;
  schedule: string;
  run_mode: RunMode;
  /** FR-04：仅告知级通知（转存成功摘要）开关；需处理级不受它影响。 */
  notify_info: boolean;
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
  /** FR-03 健康度：ok=正常 / attention=待处理 / stale=停摆，reason 是具体原因。 */
  health?: TaskHealth;
}

export interface TaskHealth {
  status: "ok" | "attention" | "stale";
  reason: string;
  fail_streak: number;
  last_status: string;
  kind: string;
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
  /** FR-02：最近一次健康检查结论；false = Cookie 失效，需更新 */
  check_ok?: boolean;
  check_message?: string;
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

/** 一个搜索引擎实例：同一协议可配多条，互为备份。 */
export interface SearchEngine {
  id: string;
  type: string;
  name: string;
  server: string;
  enable: boolean;
  username?: string;
  password?: string;
  token?: string;
  [key: string]: unknown;
}

export interface EngineFieldSpec {
  key: string;
  label: string;
  required: boolean;
  secret: boolean;
}

/** GET /api/search/engine-types：协议自描述，引擎表单按它渲染。 */
export interface EngineTypeSpec {
  type: string;
  label: string;
  default_server: string;
  fields: EngineFieldSpec[];
}

export interface SearchSource {
  engines: SearchEngine[];
}

export interface SearchError {
  engine: string;
  reason: string;
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
  /** FR-10：命名正则没命中的样本（最多 20 个）。有样本 = 正则写错了，为空 = 真的没更新。 */
  unmatched_samples?: string[];
}

/** GET /api/settings/magic/expand 的响应。 */
export interface MagicExpand {
  ok: boolean;
  name: string;
  pattern: string;
  replace: string;
}

/** FR-04：免打扰窗口。需处理级在窗口内不丢，攒到次日合成一条摘要补发。 */
export interface NotifyQuiet {
  enabled: boolean;
  /** HH:mm，跨天窗口（起 > 止）按"过夜"理解。 */
  start: string;
  end: string;
}

export interface Settings {
  crontab: string;
  push_config: PushConfig;
  magic_regex: MagicRegex;
  source: SearchSource;
  notify_enabled: boolean;
  notify_quiet: NotifyQuiet;
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

/** GET /api/settings/notify/pending：免打扰队列现状。 */
export interface NotifyPending {
  ok: boolean;
  /** 当前时刻是否落在免打扰窗口内。 */
  quiet: boolean;
  count: number;
  action: number;
  info: number;
  config: NotifyQuiet;
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
  /** 逐源失败说明：哪些引擎没出结果、为什么。 */
  errors?: SearchError[];
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
  /** true = 桌面客户端（本机运行）；false = 服务器/NAS 容器 */
  desktop_mode?: boolean;
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

/* ---------------------------------- AI 助手 ---------------------------------- */

export interface AiConfig {
  enabled: boolean;
  provider: string;
  base_url: string;
  model: string;
  has_key: boolean;
  api_key_masked: string;
  temperature: number;
  timeout_ms: number;
  mode: "chat" | "copilot";
  monthly_token_limit: number;
  tool_call: boolean;
}

export interface AiAction {
  id: string;
  kind: string;
  summary: string;
  params: Record<string, string | number | boolean>;
  impact: string;
  risk: "low" | "medium" | "high";
  needs_confirm: boolean;
  status: "pending" | "running" | "done" | "failed";
  result: string;
  edited?: boolean;
}

export interface AiSource {
  type: string;
  ref: string;
  as_of: string;
}

export interface AiMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  sources: AiSource[];
  actions: AiAction[];
  confidence: string;
  model: string;
  prompt_version: string;
  tools_version: string;
  feedback: string;
  error: string;
  created_at: string;
}

export interface AiSession {
  id: number;
  title: string;
  mode: string;
  last_at: string;
}

/** SSE 事件：delta=文本增量，tool=工具调用，done=收口，error=失败。 */
export interface AgentEvent {
  type: "delta" | "tool" | "done" | "error";
  text?: string;
  name?: string;
  status?: string;
  summary?: string;
  message?: string;
  kind?: string;
  message_id?: number;
  answer?: string;
  sources?: AiSource[];
  actions?: AiAction[];
  confidence?: string;
}

export interface AiUsageModel {
  calls: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface AiUsage {
  since: string;
  calls: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
  by_model: Record<string, AiUsageModel>;
}

/** FR-05：备份文件结构。 */
export interface BackupPayload {
  meta: {
    kind: string;
    version: string;
    exported_at: string;
    mode: "safe" | "full";
    credentials_included: boolean;
    counts: { tasks: number; accounts: number; settings: number; downloads: number };
  };
  tasks: Record<string, unknown>[];
  accounts: Record<string, unknown>[];
  settings: Record<string, unknown>;
  downloads: Record<string, unknown>[];
}

export interface BackupRestoreCounts {
  tasks: number;
  accounts: number;
  downloads: number;
  settings: number;
}

/* ---------------------------------- 总览 ---------------------------------- */

export interface OverviewIssue {
  id: number;
  taskname: string;
  status: string;
  kind: string;
  reason: string;
  fail_streak: number;
}

export interface OverviewDisk {
  known: boolean;
  target: string;
  free?: number;
  total?: number;
  free_text?: string;
  total_text?: string;
  used_pct?: number;
  will_block?: boolean;
  note?: string;
  reason?: string;
}

export interface Overview {
  generated_at: string;
  level: "ok" | "attention" | "critical" | "unknown";
  blocking: string[];
  counts: {
    tasks: number;
    active_tasks: number;
    accounts: number;
    enabled_accounts: number;
  };
  issues: OverviewIssue[];
  issues_total: number;
  bad_accounts: { id: number | null; name: string; message: string }[];
  downloads: {
    in_flight: number;
    interrupted: number;
    failed: number;
    done: number;
    done_today: number;
    failed_today: number;
  };
  disk: OverviewDisk;
  last_run: {
    task_id: number;
    taskname: string;
    status: string;
    message: string;
    at: string;
  } | null;
}

/* -------------------------------- 命名模板（FR-10） ------------------------------- */

export interface NameTemplate {
  id: string;
  name: string;
  desc: string;
  pattern: string;
  replace: string;
  samples?: string[];
}

export interface NameTemplatePreview {
  before: string;
  after: string;
  matched: boolean;
  changed: boolean;
  /** 含 {I} 占位：序号要等转存时按目标目录现状推算，预览里算不出来。 */
  pending_index: boolean;
}

/** 试跑结果里新增：命名正则没命中的样本（FR-10，用于区分"正则写错"与"真没更新"）。 */
export interface DryRunUnmatched {
  samples: string[];
}
