<script setup lang="ts">
/** FR-09 总览：一屏回答「追更还好吗」，不用跨四个页面拼信息。 */
import { onMounted, onUnmounted, ref } from "vue";
import { api } from "../api/client";
import { formatDateTime } from "../utils";
import type { Overview } from "../api/types";

const data = ref<Overview | null>(null);
const loading = ref(true);
const error = ref("");
let timer: number | undefined;

async function load() {
  try {
    const r = await api.overview();
    if (r.ok) {
      data.value = r.data;
      error.value = "";
    } else {
      error.value = r.message || "总览读取失败";
    }
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void load();
  timer = window.setInterval(load, 30000);
});
onUnmounted(() => window.clearInterval(timer));

const LEVEL_TEXT: Record<string, string> = {
  ok: "一切正常",
  attention: "有任务需要留意",
  critical: "有需要你处理的事",
  unknown: "状态未知",
};

const RUN_TEXT: Record<string, string> = {
  updated: "有新增转存",
  no_changes: "无新增",
  banned: "分享失效",
  network: "网络异常",
  failed: "失败",
  quota: "空间不足",
  skipped: "跳过",
  error: "异常",
};

function kindText(kind: string) {
  return (
    ({ banned: "分享失效", account: "账号失效", failing: "连续失败", stale: "长期未运行" } as Record<string, string>)[
      kind
    ] || kind
  );
}

function pct(n: number) {
  return Math.max(0, Math.min(100, Math.round(n)));
}
</script>

<template>
  <div class="page">
    <div class="head">
      <h2>总览</h2>
      <span v-if="data" class="stamp text-muted"> 更新于 {{ formatDateTime(data.generated_at) }} </span>
    </div>

    <div v-if="loading" class="text-muted">加载中…</div>
    <div v-else-if="error" class="err">{{ error }}</div>

    <template v-else-if="data">
      <!-- 结论卡：先回答"要紧吗"，再说细节 -->
      <div class="verdict" :class="`verdict--${data.level}`">
        <div class="verdict__title">
          <span class="verdict__dot" />
          {{ LEVEL_TEXT[data.level] || data.level }}
        </div>
        <ul v-if="data.blocking.length" class="verdict__list">
          <li v-for="b in data.blocking" :key="b">{{ b }}</li>
        </ul>
        <p v-else-if="data.issues_total" class="verdict__note">
          有 {{ data.issues_total }} 个任务需要留意，但不影响其它任务继续跑。
        </p>
        <p v-else class="verdict__note">没有待处理事项，任务与账号都在正常工作。</p>
      </div>

      <div class="grid">
        <div class="card stat">
          <div class="stat__label">追更任务</div>
          <div class="stat__value">
            {{ data.counts.active_tasks }}<span class="unit">/{{ data.counts.tasks }}</span>
          </div>
          <div class="stat__foot">
            <RouterLink v-if="data.issues_total" class="warn-link" to="/tasks">
              {{ data.issues_total }} 个待处理 →
            </RouterLink>
            <span v-else class="text-muted">全部正常</span>
          </div>
        </div>

        <div class="card stat">
          <div class="stat__label">网盘账号</div>
          <div class="stat__value">
            {{ data.counts.enabled_accounts }}<span class="unit">/{{ data.counts.accounts }}</span>
          </div>
          <div class="stat__foot">
            <RouterLink v-if="data.bad_accounts.length" class="warn-link" to="/accounts">
              {{ data.bad_accounts.length }} 个需更新 →
            </RouterLink>
            <span v-else class="text-muted">凭据有效</span>
          </div>
        </div>

        <div class="card stat">
          <div class="stat__label">今日下载</div>
          <div class="stat__value">{{ data.downloads.done_today }}</div>
          <div class="stat__foot">
            <span v-if="data.downloads.in_flight" class="text-muted"> {{ data.downloads.in_flight }} 个进行中 </span>
            <span v-else-if="data.downloads.failed_today" class="warn-link">
              失败 {{ data.downloads.failed_today }}
            </span>
            <span v-else-if="data.downloads.interrupted" class="warn-link">
              {{ data.downloads.interrupted }} 个可继续
            </span>
            <span v-else class="text-muted">暂无进行中</span>
          </div>
        </div>

        <div class="card stat">
          <div class="stat__label">磁盘可用</div>
          <div class="stat__value">
            <template v-if="data.disk.known">{{ data.disk.free_text }}</template>
            <template v-else>—</template>
          </div>
          <div class="stat__foot">
            <span v-if="data.disk.known" class="text-muted">
              共 {{ data.disk.total_text }} · 已用 {{ data.disk.used_pct }}%
            </span>
            <span v-else class="text-muted">{{ data.disk.reason || "未知" }}</span>
          </div>
          <div v-if="data.disk.known" class="bar">
            <div class="bar__fill" :style="{ width: pct(data.disk.used_pct || 0) + '%' }" />
          </div>
        </div>
      </div>

      <div v-if="data.bad_accounts.length" class="card">
        <div class="card__head">
          <h3>需更新的账号</h3>
          <RouterLink class="link" to="/accounts">去账号页 →</RouterLink>
        </div>
        <div v-for="a in data.bad_accounts" :key="a.id ?? a.name" class="row">
          <span class="badge badge--danger">需更新</span>
          <span class="row__name">{{ a.name }}</span>
          <span class="row__msg text-muted">{{ a.message }}</span>
        </div>
      </div>

      <div v-if="data.issues.length" class="card">
        <div class="card__head">
          <h3>待处理任务</h3>
          <RouterLink class="link" to="/tasks">
            {{ data.issues_total > data.issues.length ? `查看全部 ${data.issues_total} 个` : "去任务页" }} →
          </RouterLink>
        </div>
        <div v-for="i in data.issues" :key="i.id" class="row">
          <span class="badge" :class="i.status === 'attention' ? 'badge--danger' : 'badge--warn'">
            {{ kindText(i.kind) }}
          </span>
          <span class="row__name">{{ i.taskname }}</span>
          <span class="row__msg text-muted">
            {{ i.reason }}<template v-if="i.fail_streak">（连续 {{ i.fail_streak }} 次）</template>
          </span>
        </div>
      </div>

      <div class="card">
        <div class="card__head"><h3>最近一次运行</h3></div>
        <div v-if="data.last_run" class="row">
          <span class="row__name">{{ data.last_run.taskname }}</span>
          <span class="text-muted">{{ RUN_TEXT[data.last_run.status] || data.last_run.status }}</span>
          <span class="row__msg text-muted">
            {{ data.last_run.message }} · {{ formatDateTime(data.last_run.at) }}
          </span>
        </div>
        <div v-else class="text-muted">还没有运行记录。</div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.head h2 {
  margin: 0;
  font-size: 18px;
}
.stamp {
  font-size: 12px;
}

.verdict {
  border-radius: 10px;
  padding: 14px 18px;
  border: 1px solid var(--border);
  background: #fff;
}
.verdict--ok {
  border-color: #cfe9d8;
  background: #f2fbf5;
}
.verdict--attention {
  border-color: #f2dfbe;
  background: #fdf8ee;
}
.verdict--critical {
  border-color: #f7cbc9;
  background: #fdeceb;
}
.verdict__title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 15px;
}
.verdict__dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--text-muted);
}
.verdict--ok .verdict__dot {
  background: var(--success);
}
.verdict--attention .verdict__dot {
  background: var(--warning);
}
.verdict--critical .verdict__dot {
  background: var(--danger);
}
.verdict__list {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.8;
}
.verdict__note {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--text-muted);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}
.card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
}
.card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.card__head h3 {
  margin: 0;
  font-size: 14px;
}
.stat__label {
  font-size: 12px;
  color: var(--text-muted);
}
.stat__value {
  font-size: 24px;
  font-weight: 700;
  margin: 4px 0 2px;
  font-variant-numeric: tabular-nums;
}
.unit {
  font-size: 13px;
  font-weight: 400;
  color: var(--text-muted);
}
.stat__foot {
  font-size: 12px;
}
.bar {
  margin-top: 8px;
  height: 5px;
  border-radius: 3px;
  background: var(--border);
  overflow: hidden;
}
.bar__fill {
  height: 100%;
  background: var(--primary);
}
.warn-link {
  color: var(--danger);
  text-decoration: none;
}
.link {
  font-size: 12px;
  color: var(--primary);
  text-decoration: none;
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
  border-top: 1px solid var(--border);
  font-size: 13px;
  flex-wrap: wrap;
}
.row:first-of-type {
  border-top: none;
}
.row__name {
  font-weight: 600;
}
.row__msg {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.badge {
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 12px;
  background: #f2f4f7;
  color: #5b6270;
  flex-shrink: 0;
}
.badge--danger {
  background: #fdeceb;
  color: var(--danger);
}
.badge--warn {
  background: #fdf8ee;
  color: #9a6b12;
}
.err {
  color: var(--danger);
}
</style>
