<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
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
  return ({ queued: "排队", downloading: "下载中", paused: "已暂停", stopped: "已停止", done: "完成", failed: "失败", skipped: "跳过" } as Record<string, string>)[s] || s;
}

async function act(row: DownloadJob, action: "stop" | "pause" | "resume") {
  try { await api.downloadAction(row.id, action, row.source); refresh(); }
  catch (e) { ElMessage.error((e as Error).message); }
}
async function del(row: DownloadJob) {
  try { await api.deleteDownload(row.id, row.source); refresh(); }
  catch (e) { ElMessage.error((e as Error).message); }
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
      <el-table-column label="操作" width="220">
        <template #default="{ row }">
          <template v-if="row.status === 'downloading' || row.status === 'queued' || row.status === 'paused'">
            <el-button v-if="row.source === 'aria2' && row.status !== 'paused'" size="small" text @click="act(row,'pause')">暂停</el-button>
            <el-button v-if="row.source === 'aria2' && row.status === 'paused'" size="small" text @click="act(row,'resume')">继续</el-button>
            <el-button size="small" text type="warning" @click="act(row,'stop')">停止</el-button>
          </template>
          <el-button size="small" text type="danger" @click="del(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.err { color: var(--danger); font-size: 13px; margin-left: 12px; }
.sub { font-size: 12px; color: var(--text-muted); }
</style>
