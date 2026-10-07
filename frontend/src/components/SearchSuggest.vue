<script setup lang="ts">
import { ref, watch, computed } from "vue";
import { api } from "../api/client";
import { useSettingsStore } from "../stores/settings";
import type { SearchError, Suggestion } from "../api/types";

// taskname = 用户输入的关键词/任务名；选中结果不再覆盖它（仅在输入为空时用资源标题兜底）。
const taskname = defineModel<string>("taskname", { default: "" });
const shareurl = defineModel<string>("shareurl", { default: "" });

const PREF_KEY = "search.engine-pref";
const settings = useSettingsStore();

const engineOptions = computed(() =>
  (settings.settings?.source?.engines ?? [])
    .filter((e) => e.enable !== false)
    .map((e) => ({ id: e.id, name: e.name || e.type })),
);

// "" = 全部启用引擎聚合；记住上次挑的那个，只服务于下次建表单，不写进任务
const engine = ref(String(localStorage.getItem(PREF_KEY) || ""));

function rememberEngine(id: string) {
  engine.value = id;
  localStorage.setItem(PREF_KEY, id);
}

watch(
  // 停用到设置到手为止：GET 未到就重置会把有效偏好抹掉；到了又校验一次，被删/被停用的引擎不会留着继续搜
  [engineOptions, () => settings.settings],
  ([opts, loaded]) => {
    if (loaded && engine.value && !opts.some((o) => o.id === engine.value)) rememberEngine("");
  },
  { immediate: true },
);

const suggestions = ref<Suggestion[]>([]);
const searchErrors = ref<SearchError[]>([]);
const open = ref(false);
const searching = ref(false);
const searched = ref(false);
const pickedTitle = ref("");
const validating = ref(false);
const validateMsg = ref("");
const validateOk = ref(false);
const validatePending = ref(false);
let timer: ReturnType<typeof setTimeout> | undefined;
let suppressWatch = false; // 兜底回填 taskname 时，跳过一次自动搜索

function doSearch() {
  if (timer) clearTimeout(timer);
  const q = (taskname.value || "").trim();
  if (q.length < 2) {
    suggestions.value = [];
    open.value = false;
    return;
  }
  searching.value = true;
  open.value = true;
  api
    .suggestions(q, false, engine.value)
    .then((r) => {
      suggestions.value = r.data.slice(0, 12);
      searchErrors.value = r.errors ?? [];
    })
    .catch(() => {
      suggestions.value = [];
      searchErrors.value = [];
    })
    .finally(() => {
      searching.value = false;
      searched.value = true;
    });
}

function pickEngine(id: string) {
  rememberEngine(id);
  if ((taskname.value || "").trim().length >= 2) doSearch();
}

// 防抖：停止输入 700ms 后才发一次搜索，避免每字一请求
watch(taskname, () => {
  if (suppressWatch) {
    suppressWatch = false;
    return;
  }
  if (timer) clearTimeout(timer);
  const q = (taskname.value || "").trim();
  if (q.length < 2) {
    suggestions.value = [];
    open.value = false;
    return;
  }
  timer = setTimeout(doSearch, 700);
});

function pick(s: Suggestion) {
  if (timer) clearTimeout(timer);
  shareurl.value = s.shareurl;
  pickedTitle.value = s.taskname;
  if (!(taskname.value || "").trim()) {
    suppressWatch = true; // 空则兜底回填，但不触发重新搜索
    taskname.value = s.taskname;
  }
  suggestions.value = [];
  open.value = false;
  runValidate();
}

function clearPick() {
  shareurl.value = "";
  pickedTitle.value = "";
  validating.value = false;
  validateMsg.value = "";
  validateOk.value = false;
  validatePending.value = false;
}

async function runValidate() {
  if (!shareurl.value) return;
  validating.value = true;
  validateMsg.value = "";
  validateOk.value = false;
  validatePending.value = false;
  try {
    const r = await api.validateShare(shareurl.value);
    if (r.ok) {
      validateOk.value = true;
      validateMsg.value = `链接有效，共 ${r.count} 个文件`;
    } else if (r.pending) {
      validatePending.value = true;
      validateMsg.value = "该网盘即将支持";
    } else {
      validateMsg.value = r.message || "链接校验失败";
    }
  } catch (e) {
    validateMsg.value = (e as Error).message;
  } finally {
    validating.value = false;
  }
}

defineExpose({ runValidate });
</script>

<template>
  <div class="suggest">
    <el-input
      v-model="taskname"
      placeholder="任务名 / 搜索关键词（≥2字，停止输入自动搜）"
      :loading="searching"
      clearable
      @focus="open = suggestions.length > 0"
      @keyup.enter="doSearch"
    >
      <template #prepend>
        <el-select
          :model-value="engine"
          class="engine"
          placeholder="全部引擎"
          @update:model-value="(v: string) => pickEngine(v)"
        >
          <el-option label="全部引擎" value="" />
          <el-option v-for="o in engineOptions" :key="o.id" :label="o.name" :value="o.id" />
        </el-select>
      </template>
      <template #append>
        <el-button :loading="searching" @click="doSearch">搜索</el-button>
      </template>
    </el-input>

    <div v-if="searchErrors.length && suggestions.length" class="hint text-muted">
      {{ searchErrors.map((e) => `${e.engine}：${e.reason}`).join("；") }}
    </div>

    <ul v-if="open && suggestions.length" class="panel">
      <li v-for="(s, i) in suggestions" :key="i" class="item" @click="pick(s)">
        <div class="item__top">
          <span class="item__title">{{ s.taskname }}</span>
          <span class="badge" :class="s.source.includes('+') ? 'badge--primary' : 'badge--muted'">
            {{ s.source }}
          </span>
        </div>
        <div class="item__sub">
          <span class="text-muted">{{ s.datetime }}</span>
          <span v-if="s.content" class="item__desc">{{ s.content }}</span>
        </div>
      </li>
    </ul>
    <div v-else-if="open && !searching && searched" class="panel panel--empty">
      <template v-if="!engineOptions.length"> 还没有启用中的搜索引擎，请到「设置 → 资源搜索源」添加 </template>
      <template v-else-if="searchErrors.length">
        <div v-for="(e, i) in searchErrors" :key="i">{{ e.engine }}：{{ e.reason }}</div>
      </template>
      <template v-else>未搜到资源（公共搜索源可能限流），可点「搜索」重试或手动填写链接</template>
    </div>

    <!-- 已选资源：单独展示，不覆盖任务名；可清除 / 重新校验 -->
    <div v-if="shareurl" class="picked">
      <div class="picked__main">
        <span class="picked__title">{{ pickedTitle || "已选分享" }}</span>
        <span class="picked__url text-muted">{{ shareurl }}</span>
      </div>
      <div class="picked__side">
        <span v-if="validating" class="v-pending">校验中…</span>
        <span v-else-if="validateOk" class="v-ok">✅ {{ validateMsg }}</span>
        <span v-else-if="validatePending" class="v-pending">{{ validateMsg }}</span>
        <span v-else-if="validateMsg" class="v-err">❌ {{ validateMsg }}</span>
        <el-button size="small" text :loading="validating" @click="runValidate"> 重新校验 </el-button>
        <el-button size="small" text type="danger" @click="clearPick"> 清除 </el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.suggest {
  position: relative;
}
.engine {
  width: 132px;
}
.hint {
  margin-top: 4px;
  font-size: 12px;
}
.panel {
  list-style: none;
  margin: 4px 0 0;
  padding: 4px;
  position: absolute;
  z-index: 30;
  top: 100%;
  left: 0;
  right: 0;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
  max-height: 280px;
  overflow-y: auto;
}
.item {
  padding: 8px;
  border-radius: 6px;
  cursor: pointer;
}
.item:hover {
  background: var(--primary-soft);
}
.item__top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.item__title {
  font-weight: 600;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.item__sub {
  display: flex;
  gap: 10px;
  font-size: 12px;
  margin-top: 2px;
}
.item__desc {
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 60%;
}
.panel--empty {
  position: static;
  color: var(--text-muted);
  font-size: 12px;
  padding: 8px;
}
.picked {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fbfcfe;
  flex-wrap: wrap;
}
.picked__main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}
.picked__title {
  font-weight: 600;
  font-size: 13px;
}
.picked__url {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.picked__side {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.v-ok {
  color: #1c9e6e;
}
.v-pending {
  color: var(--warn);
}
.v-err {
  color: var(--danger);
}
</style>
