import type {
  Account,
  AccountActionResult,
  AccountPayload,
  AgentEvent,
  AutostartState,
  PrebackupResult,
  UpdateCheck,
  AiConfig,
  BackupPayload,
  BackupRestoreCounts,
  AiMessage,
  AiSession,
  AiUsage,
  DirList,
  DownloadEngine,
  DownloadHistoryQuery,
  DownloadJob,
  FsDirListing,
  FsRevealResult,
  FsRoot,
  DownloadRecord,
  DriverInfo,
  EngineTypeSpec,
  DryRunResult,
  HealthResponse,
  LogEntry,
  MagicExpand,
  NameTemplate,
  NameTemplatePreview,
  NotifyPending,
  NotifyTestResponse,
  Overview,
  RunSummary,
  SchedulerInfo,
  SettingKey,
  Settings,
  SharePreview,
  SharePreviewPayload,
  SuggestionResponse,
  Task,
  TaskPayload,
  ValidateResponse,
} from "./types";

/** 统一 API 客户端：JSON 请求 + SSE 流。所有路径以 /api 开头，由 vite 代理到后端。 */

/**
 * 普通请求的超时上限。没有它，一个挂起的请求会让调用方的 loading 永久为真，
 * v-loading 的蒙层就一直盖在界面上——表现为"点任何选项都没反应，但窗口还能关"。
 * 超时后走 catch 分支，调用方的 finally 得以复位 loading，蒙层自然撤掉。
 */
const REQUEST_TIMEOUT_MS = 60_000;

async function request<T>(path: string, options: RequestInit & { timeoutMs?: number } = {}): Promise<T> {
  const { timeoutMs = REQUEST_TIMEOUT_MS, ...init } = options;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  let resp: Response;
  try {
    resp = await fetch(path, {
      headers: { "content-type": "application/json", ...(init.headers || {}) },
      ...init,
      signal: controller.signal,
    });
  } catch {
    if (controller.signal.aborted) {
      throw new Error(`请求超时（${Math.round(timeoutMs / 1000)} 秒），请重试或检查后端`);
    }
    throw new Error("无法连接到后端服务，请确认已启动");
  } finally {
    window.clearTimeout(timer);
  }
  if (!resp.ok) {
    let detail = "";
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body);
    } catch {
      detail = await resp.text().catch(() => "");
    }
    throw new Error(detail || `${resp.status} ${resp.statusText}`);
  }
  return resp.json() as Promise<T>;
}

/** 读取 POST SSE 流（EventSource 仅支持 GET，运行任务用 fetch 手动解析）。 */
async function readSseStream<T>(
  path: string,
  body: unknown,
  onEntry: (entry: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  let resp: Response;
  try {
    resp = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    throw new Error("无法连接到后端服务，请确认已启动");
  }
  if (!resp.ok || !resp.body) {
    const text = await resp.text().catch(() => "");
    throw new Error(text || `${resp.status} ${resp.statusText}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of chunk.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          onEntry(JSON.parse(payload) as T);
        } catch {
          /* 忽略非 JSON 心跳/坏行 */
        }
      }
    }
  }
}

export const api = {
  // 任务
  listTasks: () => request<Task[]>("/api/tasks"),
  createTask: (body: TaskPayload) => request<Task>("/api/tasks", { method: "POST", body: JSON.stringify(body) }),
  updateTask: (id: number, body: TaskPayload) =>
    request<Task>(`/api/tasks/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteTask: (id: number) => request<{ ok: boolean }>(`/api/tasks/${id}`, { method: "DELETE" }),
  setTaskPosition: (id: number, where: "top" | "bottom") =>
    request<{ ok: boolean; sort_order: number }>(`/api/tasks/${id}/position?where=${where}`, { method: "POST" }),
  runAllTasks: (onEntry: (e: LogEntry) => void, signal?: AbortSignal) =>
    readSseStream("/api/tasks/run", undefined, onEntry, signal),
  runTask: (id: number, onEntry: (e: LogEntry) => void, signal?: AbortSignal) =>
    readSseStream(`/api/tasks/${id}/run`, undefined, onEntry, signal),
  /** 只读试跑：守卫分支（无支持的驱动 / 无可用账号）只回 ok/status/message/items，几个计数键缺席。 */
  dryRun: (body: TaskPayload) =>
    request<DryRunResult>("/api/tasks/dry-run", { method: "POST", body: JSON.stringify(body) }),
  magicExpand: (name: string) => request<MagicExpand>(`/api/settings/magic/expand?name=${encodeURIComponent(name)}`),

  // 账号
  listAccounts: () => request<Account[]>("/api/accounts"),
  createAccount: (body: AccountPayload) =>
    request<Account>("/api/accounts", { method: "POST", body: JSON.stringify(body) }),
  updateAccount: (id: number, body: AccountPayload) =>
    request<Account>(`/api/accounts/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteAccount: (id: number) => request<{ ok: boolean }>(`/api/accounts/${id}`, { method: "DELETE" }),
  refreshAccounts: () => request<AccountActionResult[]>("/api/accounts/refresh", { method: "POST" }),
  signAccounts: () => request<AccountActionResult[]>("/api/accounts/sign", { method: "POST" }),

  // 设置
  getSettings: () => request<Settings>("/api/settings"),
  putSetting: <V = unknown>(key: SettingKey, value: V) =>
    request<{ key: string; value: unknown }>(`/api/settings/${key}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),
  notifyTest: (channel?: string) =>
    request<NotifyTestResponse>("/api/settings/notify-test", {
      method: "POST",
      body: JSON.stringify(channel ? { channel } : {}),
    }),
  /** FR-04：免打扰队列现状（攒了几条、其中几条需处理）。 */
  notifyPending: () => request<NotifyPending>("/api/settings/notify/pending"),
  /** FR-04：立刻补发攒下的摘要；忽略当前是否在免打扰窗口内。 */
  notifyFlush: () => request<{ ok: boolean; sent: number }>("/api/settings/notify/flush", { method: "POST" }),

  // 文件
  listDir: (path: string, driver = "quark") =>
    request<DirList>(`/api/files/dir?path=${encodeURIComponent(path)}&driver=${driver}`),
  renameFile: (path: string, newName: string, driver = "quark") =>
    request<{ ok: boolean }>("/api/files/rename", {
      method: "POST",
      body: JSON.stringify({ path, new_name: newName, driver }),
    }),
  deleteFile: (path: string, purge: boolean, driver = "quark") =>
    request<{ ok: boolean }>("/api/files/delete", {
      method: "POST",
      body: JSON.stringify({ path, driver, purge }),
    }),
  sharePreview: (payload: SharePreviewPayload) =>
    request<SharePreview>("/api/files/share/preview", { method: "POST", body: JSON.stringify(payload) }),

  // 下载进度
  listDownloads: () => request<{ jobs: DownloadJob[] }>("/api/downloads"),
  downloadAction: (id: string, action: "stop" | "pause" | "resume", source: string) =>
    request<{ ok: boolean }>(`/api/downloads/${id}/${action}?source=${source}`, { method: "POST" }),
  deleteDownload: (id: string, source: string) =>
    request<{ ok: boolean }>(`/api/downloads/${id}?source=${source}`, { method: "DELETE" }),
  listDownloadHistory: (q: DownloadHistoryQuery) => {
    const p = new URLSearchParams();
    p.set("page", String(q.page ?? 1));
    p.set("page_size", String(q.page_size ?? 50));
    if (q.status) p.set("status", q.status);
    if (q.task_id) p.set("task_id", String(q.task_id));
    if (q.keyword) p.set("keyword", q.keyword);
    return request<{ items: DownloadRecord[]; total: number }>(`/api/downloads/history?${p}`);
  },
  deleteDownloadHistory: (id: number) => request<{ ok: boolean }>(`/api/downloads/history/${id}`, { method: "DELETE" }),
  retryDownloadHistory: (id: number) =>
    request<{ ok: boolean; message: string }>(`/api/downloads/history/${id}/retry`, { method: "POST" }),
  pruneDownloadHistory: (mode: "auto" | "failed" | "all") =>
    request<{ ok: boolean; removed: number }>("/api/downloads/history/prune", {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),

  // 下载引擎状态与 aria2 托管（自动启动 / 一键安装）
  downloadEngine: () => request<{ ok: boolean; data: DownloadEngine }>("/api/downloads/engine"),
  aria2Start: () => request<{ ok: boolean; message: string }>("/api/downloads/aria2/start", { method: "POST" }),
  aria2Install: () =>
    request<{ ok: boolean; message: string; path?: string }>("/api/downloads/aria2/install", {
      method: "POST",
    }),

  // 桌面外壳（仅客户端）：开机自启与系统通知
  desktopAutostart: () => request<{ ok: boolean; data: AutostartState }>("/api/desktop/autostart"),
  setDesktopAutostart: (enabled: boolean) =>
    request<{ ok: boolean; enabled: boolean; message: string }>("/api/desktop/autostart", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  desktopNotify: (title: string, message: string) =>
    request<{ ok: boolean; message: string }>("/api/desktop/notify", {
      method: "POST",
      body: JSON.stringify({ title, message }),
    }),

  // 版本检测与升级前备份：只告知不代劳，是否升级由人决定
  updateCheck: (force = false) =>
    request<{ ok: boolean; data: UpdateCheck }>(`/api/update/check?force=${force ? 1 : 0}`),
  updatePrebackup: () => request<PrebackupResult>("/api/update/prebackup", { method: "POST" }),

  // 本机文件系统：浏览目录选“下到哪里”，以及在文件管理器里打开某项所在目录
  fsRoots: () => request<{ ok: boolean; data: { roots: FsRoot[]; default: string } }>("/api/fs/roots"),
  fsDirs: (path = "") => request<{ ok: boolean; data: FsDirListing }>(`/api/fs/dirs?path=${encodeURIComponent(path)}`),
  fsReveal: (path: string) =>
    request<FsRevealResult>("/api/fs/reveal", { method: "POST", body: JSON.stringify({ path }) }),

  // 搜索
  suggestions: (q: string, d = false, engine = "") =>
    request<SuggestionResponse>(
      `/api/search/suggestions?q=${encodeURIComponent(q)}&d=${d ? 1 : 0}&engine=${encodeURIComponent(engine)}`,
    ),
  engineTypes: () => request<{ ok: boolean; data: EngineTypeSpec[] }>("/api/search/engine-types"),
  validateShare: (shareurl: string) =>
    request<ValidateResponse>("/api/search/validate", { method: "POST", body: JSON.stringify({ shareurl }) }),

  // 系统
  drivers: () => request<DriverInfo[]>("/api/drivers"),
  scheduler: () => request<SchedulerInfo>("/api/scheduler"),
  recentLogs: (limit = 200) => request<LogEntry[]>(`/api/logs?limit=${limit}`),
  health: () => request<HealthResponse>("/api/health"),

  // 对外 API Token
  listTokens: () => request<ApiTokenInfo[]>("/api/tokens"),
  createToken: (name: string) =>
    request<{ token: string; name: string }>("/api/tokens", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  deleteToken: (token: string) =>
    request<{ ok: boolean }>("/api/tokens", { method: "DELETE", body: JSON.stringify({ token }) }),

  // 旧配置迁移
  migratePreview: (config: object) =>
    request<MigratePreview>("/api/migrate/preview", { method: "POST", body: JSON.stringify({ config }) }),
  migrateImport: (config: object, overwrite = false) =>
    request<MigrateResult>("/api/migrate", { method: "POST", body: JSON.stringify({ config, overwrite }) }),

  /** 解析 summary 行 message（JSON 字符串）。 */
  parseSummary: (entry: LogEntry): RunSummary | null => {
    if (entry.level !== "summary") return null;
    try {
      return JSON.parse(entry.message) as RunSummary;
    } catch {
      return null;
    }
  },

  /** 全局日志流地址（EventSource 可直接订阅 GET）。 */
  logsStreamUrl: () => "/api/logs/stream",

  // AI 助手
  aiConfig: () => request<{ ok: boolean; data: AiConfig }>("/api/agent/config"),
  aiSaveConfig: (body: Partial<AiConfig> & { api_key?: string; clear_key?: boolean }) =>
    request<{ ok: boolean; data: AiConfig }>("/api/agent/config", { method: "PUT", body: JSON.stringify(body) }),
  aiTest: (body: Partial<AiConfig> & { api_key?: string }) =>
    request<{ ok: boolean; message: string; kind: string; tool_call: boolean }>("/api/agent/test", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  aiSessions: () => request<{ ok: boolean; data: AiSession[] }>("/api/agent/sessions"),
  aiCreateSession: (title: string) =>
    request<{ ok: boolean; data: { id: number; title: string } }>("/api/agent/sessions", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  aiDeleteSession: (id: number) => request<{ ok: boolean }>(`/api/agent/sessions/${id}`, { method: "DELETE" }),
  aiMessages: (sessionId: number) =>
    request<{ ok: boolean; data: AiMessage[] }>(`/api/agent/sessions/${sessionId}/messages`),
  aiSend: (sessionId: number, content: string, onEvent: (e: AgentEvent) => void, signal?: AbortSignal) =>
    readSseStream<AgentEvent>(`/api/agent/sessions/${sessionId}/messages`, { content }, onEvent, signal),
  aiExecuteAction: (actionId: string, params?: Record<string, string | number | boolean>) =>
    request<{ ok: boolean; message: string; started?: boolean }>(`/api/agent/actions/${actionId}/execute`, {
      method: "POST",
      body: JSON.stringify({ params: params || {} }),
    }),
  aiFeedback: (messageId: number, feedback: string) =>
    request<{ ok: boolean }>(`/api/agent/messages/${messageId}/feedback`, {
      method: "POST",
      body: JSON.stringify({ feedback }),
    }),
  aiUsage: () => request<{ ok: boolean; data: AiUsage }>("/api/agent/usage"),

  // 备份与恢复（FR-05）
  exportBackup: (mode: "safe" | "full" = "safe") =>
    request<{ ok: boolean; data: BackupPayload; version: string }>(`/api/backup/export?mode=${mode}`),
  importBackup: (payload: object) =>
    request<{ ok: boolean; message: string; restored?: BackupRestoreCounts; snapshot?: string }>("/api/backup/import", {
      method: "POST",
      body: JSON.stringify({ payload }),
    }),

  // FR-09 总览
  overview: () => request<{ ok: boolean; message?: string; data: Overview }>("/api/overview"),

  // FR-10 命名模板库
  nameTemplates: () =>
    request<{ ok: boolean; data: { builtin: NameTemplate[]; custom: NameTemplate[] } }>("/api/name-templates"),
  previewNameTemplate: (pattern: string, replace: string, samples: string[], taskname = "") =>
    request<{ ok: boolean; data: NameTemplatePreview[] }>("/api/name-templates/preview", {
      method: "POST",
      body: JSON.stringify({ pattern, replace, samples, taskname }),
    }),
  saveNameTemplate: (body: { name: string; pattern: string; replace: string; desc?: string }) =>
    request<{ ok: boolean; data: NameTemplate[] }>("/api/name-templates", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteNameTemplate: (id: string) =>
    request<{ ok: boolean; data: NameTemplate[] }>(`/api/name-templates/${id}`, { method: "DELETE" }),
};

export type { LogEntry };

export interface ApiTokenInfo {
  token_preview: string;
  name: string;
  created_at: string;
}

export interface MigratePreview {
  accounts: number;
  tasks: number;
  settings: string[];
  plugin_tasks_ignored: string[];
}

export interface MigrateResult extends MigratePreview {
  ok: boolean;
  imported_accounts: number;
  imported_tasks: number;
}
