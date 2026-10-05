<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api/client";
import { useTasksStore } from "../stores/tasks";
import type { DownloadJob, DownloadRecord } from "../api/types";

const tab = ref<"active" | "history">("active");
const tasks = useTasksStore();

/* ---------- 进行中 ---------- */
const jobs = ref<DownloadJob[]>([]);
const error = ref("");
let timer: number | undefined;

async function refresh() {
  if (document.hidden) return;
  try {
    jobs.value = (await api.listDownloads()).jobs;
    error.value = "";
  } catch (e) {
    error.value = (e as Error).message;
  }
}

async function act(row: DownloadJob, action: "stop" | "pause" | "resume") {
  try {
    await api.downloadAction(row.id, action, row.source);
    refresh();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

async function del(row: DownloadJob) {
  try {
    await api.deleteDownload(row.id, row.source);
    refresh();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

/* ---------- 历史 ---------- */
const rows = ref<DownloadRecord[]>([]);
const total = ref(0);
const hLoading = ref(false);
const query = reactive({
  page: 1,
  page_size: 50,
  status: [] as string[],
  task_id: null as number | null,
  keyword: "",
});
const statusOptions = [
  { label: "排队", value: "queued" },
  { label: "下载中", value: "downloading" },
  { label: "完成", value: "done" },
  { label: "失败", value: "failed" },
  { label: "跳过", value: "skipped" },
  { label: "已停止", value: "stopped" },
];
let hTimer: number | undefined;
let retryTimer: number | undefined;

const hasOpen = computed(() => rows.value.some((r) => r.status === "queued" || r.status === "downloading"));

function stateText(s?: string): string {
  if (s === "ok") return "在";
  if (s === "missing") return "已丢失";
  if (s === "partial") return "不完整"; // 终态行：文件大小与账本不符（如残留占位/截断）
  return "未校验"; // unknown/缺省：在途行不做到位判断，不撒谎说「在」
}

async function loadHistory() {
  // 后台标签页不轮询：不可见的表格没必要刷，后端挂了也不会每 5s 弹一次错误提示
  if (document.hidden) return;
  hLoading.value = true;
  try {
    const r = await api.listDownloadHistory({
      page: query.page,
      page_size: query.page_size,
      status: query.status.join(","),
      task_id: query.task_id,
      keyword: query.keyword.trim(),
    });
    rows.value = r.items;
    total.value = r.total;
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    hLoading.value = false;
  }
}

async function delRecord(row: DownloadRecord) {
  try {
    await api.deleteDownloadHistory(row.id);
    ElMessage.success("已删除记录（磁盘文件保留）");
    loadHistory();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

async function retry(row: DownloadRecord) {
  try {
    const r = await api.retryDownloadHistory(row.id);
    ElMessage.success(r.message);
    await loadHistory();
    // 后端先取直链、后落账本行：上面的即时刷新大概率还看不到新记录（此刻也没有未完成行，
    // 5s 轮询不会启动）。1.5s 后补刷一次让新行浮现，不用操作者再进出 tab。
    window.clearTimeout(retryTimer);
    retryTimer = window.setTimeout(loadHistory, 1500);
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

const pruneLabels = { auto: "按保留策略", failed: "失败记录", all: "全部" } as const;

async function onPrune(mode: "auto" | "failed" | "all") {
  try {
    await ElMessageBox.confirm(`确认清理${pruneLabels[mode]}的下载历史？（不会删除磁盘文件）`, "清理历史", {
      type: "warning",
    });
    const r = await api.pruneDownloadHistory(mode);
    ElMessage.success(`已清理 ${r.removed} 条`);
    loadHistory();
  } catch (e) {
    // ElMessageBox 取消时 reject "cancel"（非 Error），与 TasksView 惯例一致：静默返回
    if (e !== "cancel" && e instanceof Error) ElMessage.error(e.message);
  }
}

/** 历史轮询只在「历史 tab 在前」且「仍有未完成记录」时跑：切走即停，切回自动续上。 */
function syncHistoryPoll() {
  window.clearInterval(hTimer);
  if (tab.value === "history" && hasOpen.value) hTimer = window.setInterval(loadHistory, 5000);
}

watch(tab, (v) => {
  if (v === "history") loadHistory();
  syncHistoryPoll();
});
watch(
  () => [query.page, query.page_size],
  () => tab.value === "history" && loadHistory(),
);
// 状态/任务筛选变化必须回第 1 页：停在第 N 页可能对上空的筛选结果、总数却非零。
// page 非 1 时先归 1，交给上面的 watch 统一触发加载，避免同一次改筛选发两遍请求
// （与关键词 watcher 同款守卫）。
watch(
  () => [query.status.join(","), query.task_id],
  () => {
    const pageChanged = query.page !== 1;
    query.page = 1;
    if (!pageChanged && tab.value === "history") loadHistory();
  },
);
let kwTimer: number | undefined;
watch(
  () => query.keyword,
  () => {
    window.clearTimeout(kwTimer);
    kwTimer = window.setTimeout(() => {
      // page 非 1 时先归 1，交给下面的 watch 统一触发加载，避免同一次搜索发两遍请求
      const pageChanged = query.page !== 1;
      query.page = 1;
      if (!pageChanged && tab.value === "history") loadHistory();
    }, 400);
  },
);
watch(hasOpen, syncHistoryPoll);

function fmtSize(n: number): string {
  if (n <= 0) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}

function pct(j: DownloadJob): number {
  if (!j.total) return j.status === "done" ? 100 : 0;
  return Math.min(100, Math.round((j.done / j.total) * 100));
}

function progressStatus(s: string): "" | "success" | "exception" | "warning" {
  if (s === "done" || s === "skipped") return "success";
  if (s === "failed") return "exception";
  return "";
}

function statusText(s: string): string {
  return (
    (
      {
        queued: "排队",
        downloading: "下载中",
        paused: "已暂停",
        stopped: "已停止",
        done: "完成",
        failed: "失败",
        skipped: "跳过",
      } as Record<string, string>
    )[s] || s
  );
}

function fmtTime(iso: string | null): string {
  return iso ? iso.replace("T", " ").slice(0, 19) : "-";
}

onMounted(() => {
  refresh();
  tasks.fetchTasks();
  timer = window.setInterval(refresh, 1500);
  document.addEventListener("visibilitychange", onVisible);
});
onBeforeUnmount(() => {
  window.clearInterval(timer);
  window.clearInterval(hTimer);
  window.clearTimeout(kwTimer);
  window.clearTimeout(retryTimer);
  document.removeEventListener("visibilitychange", onVisible);
});

function onVisible() {
  if (!document.hidden) refresh();
}
</script>

<template>
  <div>
    <div class="sticky-bar">
      <span class="sticky-bar__title">下载任务</span>
      <span v-if="error" class="err">{{ error }}</span>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane name="active" :label="`进行中 (${jobs.length})`">
        <el-table :data="jobs" empty-text="暂无进行中的下载" row-key="id">
          <el-table-column prop="taskname" label="任务" min-width="120" show-overflow-tooltip />
          <el-table-column prop="filename" label="文件" min-width="200" show-overflow-tooltip />
          <el-table-column label="进度" min-width="200">
            <template #default="{ row }">
              <el-progress :percentage="pct(row)" :status="progressStatus(row.status)" :stroke-width="12" />
              <span class="sub">{{ fmtSize(row.done) }} / {{ row.total ? fmtSize(row.total) : "未知" }}</span>
            </template>
          </el-table-column>
          <el-table-column label="速度" width="110">
            <template #default="{ row }">
              {{ row.status === "downloading" ? `${fmtSize(row.speed)}/s` : "-" }}
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag
                :type="row.status === 'failed' ? 'danger' : row.status === 'done' ? 'success' : 'info'"
                size="small"
              >
                {{ statusText(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="dest_path" label="目标路径" min-width="220" show-overflow-tooltip />
          <el-table-column label="操作" width="220">
            <template #default="{ row }">
              <template v-if="row.status === 'downloading' || row.status === 'queued' || row.status === 'paused'">
                <el-button
                  v-if="row.source === 'aria2' && row.status !== 'paused'"
                  size="small"
                  text
                  @click="act(row, 'pause')"
                  >暂停</el-button
                >
                <el-button
                  v-if="row.source === 'aria2' && row.status === 'paused'"
                  size="small"
                  text
                  @click="act(row, 'resume')"
                  >继续</el-button
                >
                <el-button size="small" text type="warning" @click="act(row, 'stop')">停止</el-button>
              </template>
              <el-button size="small" text type="danger" @click="del(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane name="history" label="历史">
        <div class="filters">
          <el-select
            v-model="query.status"
            multiple
            collapse-tags
            placeholder="全部状态"
            clearable
            style="width: 200px"
          >
            <el-option v-for="o in statusOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
          <el-select v-model="query.task_id" placeholder="全部任务" clearable filterable style="width: 200px">
            <el-option v-for="t in tasks.sorted" :key="t.id" :label="t.taskname" :value="t.id" />
          </el-select>
          <el-input v-model="query.keyword" placeholder="文件名 / 路径" clearable style="width: 240px" />
          <el-dropdown @command="onPrune">
            <el-button size="small">清理</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="auto">按保留策略清理</el-dropdown-item>
                <el-dropdown-item command="failed">只清失败记录</el-dropdown-item>
                <el-dropdown-item command="all" divided>清空全部记录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>

        <el-table v-loading="hLoading" :data="rows" empty-text="暂无下载记录" row-key="id">
          <el-table-column prop="taskname" label="任务" min-width="120" show-overflow-tooltip />
          <el-table-column prop="filename" label="文件" min-width="200" show-overflow-tooltip />
          <el-table-column label="体积" width="130">
            <template #default="{ row }">
              {{ fmtSize(row.size_done) }} / {{ row.size_total ? fmtSize(row.size_total) : "未知" }}
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag
                :type="row.status === 'failed' ? 'danger' : row.status === 'done' ? 'success' : 'info'"
                size="small"
              >
                {{ statusText(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="文件" width="90">
            <template #default="{ row }">
              <span :class="row.file_state === 'missing' ? 'lost' : ''">{{ stateText(row.file_state) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="完成时间" width="160">
            <template #default="{ row }">{{ fmtTime(row.finished_at) }}</template>
          </el-table-column>
          <el-table-column prop="dest_path" label="目标路径" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="sub">{{ row.dest_path }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="170">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="retry(row)">重下</el-button>
              <el-button size="small" text type="danger" @click="delRecord(row)">删记录</el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.page_size"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          style="margin-top: 12px; justify-content: flex-end"
        />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.err {
  color: var(--danger);
  font-size: 13px;
  margin-left: 12px;
}
.sub {
  font-size: 12px;
  color: var(--text-muted);
}
.filters {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.lost {
  color: var(--danger);
  font-size: 13px;
}
</style>
