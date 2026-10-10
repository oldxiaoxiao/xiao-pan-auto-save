<script setup lang="ts">
import { ref, watch } from "vue";
import { api } from "../api/client";
import type { FsItem } from "../api/types";

const props = defineProps<{ modelValue: boolean; path?: string; driver?: string }>();
const emit = defineEmits<{ "update:modelValue": [boolean]; pick: [string] }>();

const current = ref("/");
const items = ref<FsItem[]>([]);
const loading = ref(false);
const error = ref("");
const newName = ref("");

async function load(path: string) {
  loading.value = true;
  error.value = "";
  try {
    const res = await api.listDir(path, props.driver || "quark");
    current.value = res.path || "/";
    items.value = res.list || [];
  } catch (e) {
    error.value = (e as Error).message;
    items.value = [];
  } finally {
    loading.value = false;
  }
}

function segments(): { label: string; path: string }[] {
  const parts = current.value.split("/").filter(Boolean);
  const out: { label: string; path: string }[] = [{ label: "根目录", path: "/" }];
  let acc = "";
  for (const p of parts) {
    acc += "/" + p;
    out.push({ label: p, path: acc });
  }
  return out;
}

function enter(item: FsItem) {
  const next = `${current.value.replace(/\/$/, "")}/${item.name}`;
  void load(next);
}

function confirm() {
  const base = current.value.replace(/\/$/, "") || "/";
  const name = newName.value.trim().replace(/^\/+|\/+$/g, "");
  emit("pick", name ? `${base}/${name}` : base);
  emit("update:modelValue", false);
  newName.value = "";
}

function close() {
  emit("update:modelValue", false);
}

watch(
  () => props.modelValue,
  (v) => {
    if (v) void load(props.path && props.path.startsWith("/") ? props.path : "/");
  },
);
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="选择网盘保存目录"
    width="560px"
    append-to-body
    @update:model-value="(v: boolean) => (v ? null : close())"
  >
    <div class="crumbs">
      <button v-for="(s, i) in segments()" :key="i" class="crumb" @click="load(s.path)">
        {{ s.label }}
      </button>
    </div>

    <p v-if="error" class="err">{{ error }}</p>

    <div v-loading="loading" class="list">
      <div v-if="!items.length && !loading" class="empty">该目录下暂无子目录</div>
      <button
        v-for="it in items.filter((x) => x.is_dir)"
        :key="it.fid"
        class="row"
        @click="enter(it)"
      >
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6">
          <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
        </svg>
        <span class="row__name">{{ it.name }}</span>
        <span class="row__go">进入</span>
      </button>
    </div>

    <div class="foot">
      <span class="foot__label">保存到</span>
      <code class="foot__path">{{ current }}</code>
      <input v-model="newName" class="input" placeholder="新建子目录名（可留空）" />
      <span class="hint">转存时目录不存在会自动创建</span>
    </div>

    <template #footer>
      <button class="btn" @click="close">取消</button>
      <button class="btn btn--primary" @click="confirm">使用此目录</button>
    </template>
  </el-dialog>
</template>

<style scoped>
.crumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 10px;
}
.crumb {
  border: none;
  background: transparent;
  color: var(--primary);
  font-size: 12px;
  cursor: pointer;
  padding: 2px 4px;
}
.list {
  max-height: 320px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  border: none;
  background: transparent;
  padding: 8px 10px;
  font-size: 13px;
  cursor: pointer;
  text-align: left;
  border-bottom: 1px solid var(--border);
}
.row:last-child {
  border-bottom: none;
}
.row:hover {
  background: var(--primary-soft);
}
.row__name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row__go {
  font-size: 11px;
  color: var(--text-muted);
}
.empty {
  padding: 24px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12px;
}
.foot {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.foot__label {
  font-size: 12px;
  color: var(--text-muted);
}
.foot__path {
  background: #f0f2f5;
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 12px;
  word-break: break-all;
}
.input {
  flex: 1;
  min-width: 160px;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 5px 8px;
  font-size: 12px;
}
.hint {
  font-size: 11px;
  color: var(--text-muted);
}
.err {
  color: var(--danger);
  font-size: 12px;
}
.btn {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 8px;
  padding: 6px 14px;
  font-size: 13px;
  cursor: pointer;
}
.btn--primary {
  background: var(--primary);
  border-color: var(--primary);
  color: #fff;
}
</style>
