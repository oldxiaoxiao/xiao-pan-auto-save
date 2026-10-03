import { defineStore } from "pinia";
import { ref, shallowRef } from "vue";
import { api } from "../api/client";
import type { LogEntry } from "../api/types";

const MAX_BUFFER = 500;

/**
 * 全局日志流 store：用 EventSource 订阅 GET /api/logs/stream（后端先回放最近 50 条再实时推送）。
 * 环形缓冲 500 条，引用计数管理多订阅者。
 */
export const useLogStreamStore = defineStore("logStream", () => {
  const entries = shallowRef<LogEntry[]>([]);
  const connected = ref(false);
  const error = ref("");
  let source: EventSource | null = null;
  let refs = 0;

  function push(entry: LogEntry) {
    const next = [...entries.value, entry];
    if (next.length > MAX_BUFFER) next.splice(0, next.length - MAX_BUFFER);
    entries.value = next;
  }

  function connect() {
    refs += 1;
    if (source) return;
    error.value = "";
    try {
      source = new EventSource(api.logsStreamUrl());
    } catch {
      error.value = "无法建立日志连接";
      return;
    }
    source.onopen = () => {
      connected.value = true;
      error.value = "";
    };
    source.onmessage = (ev: MessageEvent<string>) => {
      try {
        push(JSON.parse(ev.data) as LogEntry);
      } catch {
        /* 忽略坏行 */
      }
    };
    source.onerror = () => {
      connected.value = false;
      error.value = "日志连接中断，正在重试…";
    };
  }

  function disconnect() {
    refs = Math.max(0, refs - 1);
    if (refs === 0 && source) {
      source.close();
      source = null;
      connected.value = false;
    }
  }

  function clear() {
    entries.value = [];
  }

  return { entries, connected, error, connect, disconnect, clear };
});
