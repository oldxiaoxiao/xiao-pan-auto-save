<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { api } from "../api/client";
import type { DownloadJob } from "../api/types";

const jobs = ref<DownloadJob[]>([]);
const error = ref("");
let timer: number | undefined;

function pct(j: DownloadJob): number {
  if (!j.total) return j.status === "done" ? 100 : 0;
  return Math.min(100, Math.round((j.done / j.total) * 100));
}
function fmtSize(n: number): string {
  if (n <= 0) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}
function progressStatus(s: string): "" | "success" | "exception" | "warning" {
  if (s === "done" || s === "skipped") return "success";
  if (s === "failed") return "exception";
  return "";
}
function statusText(s: string): string {
  return ({ queued: "排队", downloading: "下载中", done: "完成", failed: "失败", skipped: "跳过" } as Record<string, string>)[s] || s;
}

async function refresh() {
  if (document.hidden) return;
  try {
    const r = await api.listDownloads();
    jobs.value = r.jobs;
    error.value = "";
  } catch (e) {
    error.value = (e as Error).message;
  }
}

function onVisible() {
  if (!document.hidden) refresh();
}

onMounted(() => {
  refresh();
  timer = window.setInterval(refresh, 1500);
  document.addEventListener("visibilitychange", onVisible);
});
onBeforeUnmount(() => {
  window.clearInterval(timer);
  document.removeEventListener("visibilitychange", onVisible);
});
</script>

<template>
  <div>
    <div class="sticky-bar">
      <span class="sticky-bar__title">下载任务</span>
      <span v-if="error" class="err">{{ error }}</span>
    </div>
    <el-table :data="jobs" empty-text="暂无下载记录" row-key="id">
      <el-table-column prop="taskname" label="任务" min-width="120" show-overflow-tooltip />
      <el-table-column prop="filename" label="文件" min-width="200" show-overflow-tooltip />
      <el-table-column label="进度" min-width="200">
        <template #default="{ row }">
          <el-progress :percentage="pct(row)" :status="progressStatus(row.status)" :stroke-width="12" />
          <span class="sub">{{ fmtSize(row.done) }} / {{ row.total ? fmtSize(row.total) : "未知" }}</span>
        </template>
      </el-table-column>
      <el-table-column label="速度" width="110">
        <template #default="{ row }">{{ row.status === "downloading" ? `${fmtSize(row.speed)}/s` : "-" }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.status === 'failed' ? 'danger' : row.status === 'done' ? 'success' : 'info'" size="small">
            {{ statusText(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="dest_path" label="目标路径" min-width="220" show-overflow-tooltip />
    </el-table>
  </div>
</template>

<style scoped>
.err { color: var(--danger); font-size: 13px; margin-left: 12px; }
.sub { font-size: 12px; color: var(--text-muted); }
</style>
