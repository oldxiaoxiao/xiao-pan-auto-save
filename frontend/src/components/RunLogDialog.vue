<script setup lang="ts">
import { ref, watch, nextTick, computed } from "vue";
import { api } from "../api/client";
import type { LogEntry, RunSummary } from "../api/types";

const props = withDefaults(defineProps<{ modelValue: boolean; taskId?: number | null; name?: string }>(), {
  taskId: null,
  name: "",
});

const emit = defineEmits<{ (e: "update:modelValue", v: boolean): void }>();

const lines = ref<LogEntry[]>([]);
const running = ref(false);
const summary = ref<RunSummary | null>(null);
const errorMsg = ref("");
const autoScroll = ref(true);
const terminal = ref<HTMLElement | null>(null);
let controller: AbortController | null = null;

const title = computed(() => (props.taskId == null ? "立即运行全部任务" : `运行《${props.name}》`));

function levelClass(level: string): string {
  const l = level.toLowerCase();
  if (l === "error") return "lv-error";
  if (l === "warn" || l === "warning") return "lv-warn";
  if (l === "done" || l === "summary") return "lv-done";
  if (l === "info") return "lv-info";
  return "lv-info";
}

function scrollBottom() {
  nextTick(() => {
    if (autoScroll.value && terminal.value) terminal.value.scrollTop = terminal.value.scrollHeight;
  });
}

async function start() {
  lines.value = [];
  summary.value = null;
  errorMsg.value = "";
  running.value = true;
  controller = new AbortController();
  const onEntry = (e: LogEntry) => {
    if (e.level === "summary") {
      summary.value = api.parseSummary(e);
      return;
    }
    lines.value.push(e);
    scrollBottom();
  };
  try {
    if (props.taskId == null) await api.runAllTasks(onEntry, controller.signal);
    else await api.runTask(props.taskId, onEntry, controller.signal);
  } catch (e) {
    errorMsg.value = (e as Error).message;
  } finally {
    running.value = false;
  }
}

function stop() {
  controller?.abort();
  running.value = false;
}

function close() {
  stop();
  emit("update:modelValue", false);
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) start();
    else stop();
  },
);
</script>

<template>
  <el-dialog :model-value="modelValue" :title="title" width="min(760px, 94vw)" top="8vh" @close="close">
    <div class="runbar">
      <el-tag v-if="running" type="primary" effect="dark" size="small"> 运行中… </el-tag>
      <el-tag v-else-if="errorMsg" type="danger" size="small"> 失败 </el-tag>
      <el-tag v-else type="success" size="small"> 已完成 </el-tag>
      <el-checkbox v-model="autoScroll" class="autoscroll"> 自动滚动 </el-checkbox>
      <el-button v-if="running" size="small" type="danger" text @click="stop"> 中止 </el-button>
    </div>

    <div v-if="errorMsg" class="err">
      {{ errorMsg }}
    </div>

    <div ref="terminal" class="terminal mono">
      <div v-for="(l, i) in lines" :key="i" class="line" :class="levelClass(l.level)">
        <span class="ts">{{ l.ts.slice(11) }}</span>
        <span class="msg">{{ l.message }}</span>
      </div>
      <div v-if="!lines.length && running" class="text-muted">等待日志…</div>
    </div>

    <div v-if="summary" class="summary">
      <div class="summary__title">本次运行统计（run_id: {{ summary.run_id }}）</div>
      <div class="stat-row">
        <span class="muted" title="本次载入的任务行数，含被跳过的行；= 实际运行 + 跳过 + 跳过已停用"
          >总数 <b>{{ summary.total }}</b></span
        >
        <span class="driven" title="本次真正驱动的任务数 = 总数 - 跳过 - 跳过已停用"
          >实际运行 <b>{{ summary.driven }}</b></span
        >
        <span class="ok"
          >更新 <b>{{ summary.updated }}</b></span
        >
        <span class="bad"
          >失败 <b>{{ summary.failed }}</b></span
        >
        <span class="muted"
          >跳过 <b>{{ summary.skipped }}</b></span
        >
        <span class="muted"
          >跳过已停用 <b>{{ summary.disabled_skipped }}</b></span
        >
      </div>
    </div>

    <template #footer>
      <el-button @click="close"> 关闭 </el-button>
      <el-button type="primary" :disabled="running" @click="start"> 重新运行 </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.runbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.autoscroll {
  margin-left: auto;
}
.err {
  color: var(--danger);
  font-size: 13px;
  margin-bottom: 8px;
}
.terminal {
  background: #0f1420;
  border-radius: 8px;
  padding: 12px 14px;
  height: 340px;
  overflow-y: auto;
  font-size: 12.5px;
  line-height: 1.7;
}
.line {
  display: flex;
  gap: 10px;
}
.ts {
  color: #5b6577;
  flex-shrink: 0;
}
.lv-info .msg {
  color: #d7dde8;
}
.lv-warn .msg {
  color: #ffcf6b;
}
.lv-error .msg {
  color: #ff7b72;
}
.lv-done {
  border-top: 1px dashed #2f6fed;
  margin-top: 4px;
  padding-top: 4px;
}
.lv-done .msg {
  color: #6ea8ff;
}
.summary {
  margin-top: 14px;
}
.summary__title {
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 8px;
}
.stat-row {
  display: flex;
  gap: 18px;
  font-size: 14px;
}
.stat-row .ok {
  color: #1c9e6e;
}
.stat-row .driven {
  color: var(--primary);
}
.stat-row .bad {
  color: var(--danger);
}
.stat-row .muted {
  color: var(--text-muted);
}
</style>
