import type {
  Account,
  AccountActionResult,
  AccountPayload,
  DirList,
  DownloadJob,
  DriverInfo,
  HealthResponse,
  LogEntry,
  NotifyTestResponse,
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
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(path, {
      headers: { "content-type": "application/json", ...(options.headers || {}) },
      ...options,
    });
  } catch {
    throw new Error("无法连接到后端服务，请确认已启动");
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
async function readSseStream(
  path: string,
  body: unknown,
  onEntry: (entry: LogEntry) => void,
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
          onEntry(JSON.parse(payload) as LogEntry);
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
  runAllTasks: (onEntry: (e: LogEntry) => void, signal?: AbortSignal) =>
    readSseStream("/api/tasks/run", undefined, onEntry, signal),
  runTask: (id: number, onEntry: (e: LogEntry) => void, signal?: AbortSignal) =>
    readSseStream(`/api/tasks/${id}/run`, undefined, onEntry, signal),

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

  // 搜索
  suggestions: (q: string, d = false) =>
    request<SuggestionResponse>(`/api/search/suggestions?q=${encodeURIComponent(q)}&d=${d ? 1 : 0}`),
  validateShare: (shareurl: string) =>
    request<ValidateResponse>("/api/search/validate", { method: "POST", body: JSON.stringify({ shareurl }) }),

  // 系统
  drivers: () => request<DriverInfo[]>("/api/drivers"),
  scheduler: () => request<SchedulerInfo>("/api/scheduler"),
  recentLogs: (limit = 200) => request<LogEntry[]>(`/api/logs?limit=${limit}`),
  health: () => request<HealthResponse>("/api/health"),

  // 对外 API Token
  listTokens: () => request<ApiTokenInfo[]>("/api/tokens"),
  createToken: (name: string) => request<{ token: string; name: string }>("/api/tokens", {
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
