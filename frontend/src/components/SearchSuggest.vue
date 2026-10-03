<script setup lang="ts">
import { ref, watch } from "vue";
import { api } from "../api/client";
import type { Suggestion } from "../api/types";

const taskname = defineModel<string>("taskname", { default: "" });
const shareurl = defineModel<string>("shareurl", { default: "" });

const suggestions = ref<Suggestion[]>([]);
const open = ref(false);
const searching = ref(false);
const searched = ref(false);
const validating = ref(false);
const validateMsg = ref("");
const validateOk = ref(false);
const validatePending = ref(false);
let timer: ReturnType<typeof setTimeout> | undefined;

watch(taskname, (q) => {
  if (timer) clearTimeout(timer);
  if (!q || q.trim().length < 2) {
    suggestions.value = [];
    open.value = false;
    return;
  }
  timer = setTimeout(async () => {
    searching.value = true;
    open.value = true;
    try {
      const r = await api.suggestions(q.trim());
      suggestions.value = r.data.slice(0, 12);
    } catch {
      suggestions.value = [];
    } finally {
      searching.value = false;
      searched.value = true;
    }
  }, 600);
});

async function pick(s: Suggestion) {
  taskname.value = s.taskname;
  shareurl.value = s.shareurl;
  open.value = false;
  await runValidate();
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
      placeholder="任务名（输入≥2字触发智能搜索）"
      :loading="searching"
      clearable
      @focus="open = suggestions.length > 0"
    />
    <ul v-if="open && suggestions.length" class="panel">
      <li v-for="(s, i) in suggestions" :key="i" class="item" @click="pick(s)">
        <div class="item__top">
          <span class="item__title">{{ s.taskname }}</span>
          <span class="badge" :class="s.source === 'CloudSaver' ? 'badge--primary' : 'badge--muted'">
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
      未搜到资源（公共搜索源可能限流），可稍后重试或手动填写链接
    </div>

    <div class="validate">
      <el-button size="small" text :loading="validating" @click="runValidate"> 校验链接 </el-button>
      <span v-if="validateOk" class="v-ok">{{ validateMsg }}</span>
      <span v-else-if="validatePending" class="v-pending">{{ validateMsg }}</span>
      <span v-else-if="validateMsg" class="v-err">{{ validateMsg }}</span>
    </div>
  </div>
</template>

<style scoped>
.suggest {
  position: relative;
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
.validate {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
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
