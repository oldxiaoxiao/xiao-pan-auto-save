<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from "vue";
import { storeToRefs } from "pinia";
import { useLogStreamStore } from "../stores/logStream";
import { relativeTime } from "../utils";
import type { LogEntry } from "../api/types";

const store = useLogStreamStore();
const { entries, connected, error } = storeToRefs(store);

const autoScroll = ref(true);
const term = ref<HTMLElement | null>(null);

function levelClass(level: string): string {
  const l = (level || "").toLowerCase();
  if (l === "error") return "lv-error";
  if (l === "warn" || l === "warning") return "lv-warn";
  if (l === "done") return "lv-done";
  if (l === "summary") return "lv-summary";
  return "lv-info";
}

// 按 run_id 分组，插入分隔头
const grouped = computed<{ runId: string; lines: LogEntry[] }[]>(() => {
  const out: { runId: string; lines: LogEntry[] }[] = [];
  for (const e of entries.value) {
    const rid = e.run_id || "";
    const last = out[out.length - 1];
    if (last && last.runId === rid) last.lines.push(e);
    else out.push({ runId: rid, lines: [e] });
  }
  return out;
});

function scrollBottom() {
  nextTick(() => {
    if (autoScroll.value && term.value) term.value.scrollTop = term.value.scrollHeight;
  });
}

watch(() => entries.value.length, scrollBottom);

onMounted(() => store.connect());
onBeforeUnmount(() => store.disconnect());
</script>

<template>
  <div>
    <div class="sticky-bar">
      <span class="sticky-bar__title">运行日志</span>
      <span class="status" :class="connected ? 'on' : 'off'">
        <i class="dot" />{{ connected ? "实时连接" : error || "未连接" }}
      </span>
      <el-checkbox v-model="autoScroll" class="as"> 自动滚动 </el-checkbox>
      <el-button size="small" text @click="store.clear()"> 清空显示 </el-button>
    </div>

    <div ref="term" class="terminal mono">
      <div v-if="!entries.length" class="empty">等待日志推送…（后端会先回放最近 50 条）</div>
      <div v-for="(g, gi) in grouped" :key="gi" class="group">
        <div v-if="g.runId" class="runbar">
          <span class="run-id">run {{ g.runId }}</span>
          <span class="text-muted">{{ relativeTime(g.lines[0]?.ts) }}</span>
        </div>
        <div v-for="(l, i) in g.lines" :key="i" class="line" :class="levelClass(l.level)">
          <span class="ts">{{ l.ts.slice(11) }}</span>
          <span class="lv">[{{ l.level }}]</span>
          <span class="msg">{{ l.message }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  margin-left: auto;
}
.status .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.status.on .dot {
  background: var(--success);
}
.status.off .dot {
  background: var(--danger);
}
.status.off {
  color: var(--danger);
}
.as {
  margin-left: 10px;
}
.terminal {
  background: #0f1420;
  border-radius: 10px;
  padding: 14px 16px;
  height: calc(100vh - 180px);
  min-height: 320px;
  overflow-y: auto;
  font-size: 12.5px;
  line-height: 1.7;
  color: #7ee787;
}
.empty {
  color: #5b6577;
}
.group {
  margin-bottom: 6px;
}
.runbar {
  display: flex;
  gap: 12px;
  color: #6ea8ff;
  border-bottom: 1px dashed #2b3550;
  margin: 6px 0;
  font-size: 12px;
}
.line {
  display: flex;
  gap: 8px;
}
.ts {
  color: #5b6577;
  flex-shrink: 0;
}
.lv {
  color: #8b94a7;
  flex-shrink: 0;
}
.lv-info .msg {
  color: #c9d1d9;
}
.lv-warn .msg {
  color: #ffcf6b;
}
.lv-error .msg {
  color: #ff7b72;
}
.lv-done {
  border-top: 1px solid #2f6fed;
  margin-top: 3px;
  padding-top: 3px;
}
.lv-done .msg {
  color: #6ea8ff;
}
.lv-summary .msg {
  color: #7ee787;
}
</style>
