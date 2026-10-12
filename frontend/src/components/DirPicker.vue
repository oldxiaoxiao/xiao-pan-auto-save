<script setup lang="ts">
import { ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api/client";
import type { FsDirListing, FsRoot } from "../api/types";

const props = defineProps<{ modelValue: string; placeholder?: string }>();
const emit = defineEmits<{ "update:modelValue": [string] }>();

const open = ref(false);
const loading = ref(false);
const current = ref("");
const listing = ref<FsDirListing | null>(null);
const roots = ref<FsRoot[]>([]);

async function load(path: string) {
  loading.value = true;
  try {
    const res = await api.fsDirs(path);
    listing.value = res.data;
    current.value = res.data.path;
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    loading.value = false;
  }
}

async function show() {
  open.value = true;
  if (!roots.value.length) {
    try {
      const res = await api.fsRoots();
      roots.value = res.data.roots;
    } catch {
      roots.value = [];
    }
  }
  await load(props.modelValue || current.value);
}

/** 双击进入子目录；列表较长时比"选中再确认"更省事。 */
function enter(path: string) {
  void load(path);
}

function goUp() {
  if (listing.value?.parent) void load(listing.value.parent);
}

function choose() {
  emit("update:modelValue", current.value);
  open.value = false;
}
</script>

<template>
  <div class="dirpicker">
    <slot name="input">
      <el-input
        :model-value="props.modelValue"
        :placeholder="props.placeholder"
        @update:model-value="emit('update:modelValue', $event)"
      />
    </slot>
    <el-button class="btn" @click="show">浏览…</el-button>

    <el-dialog v-model="open" title="选择目录" width="560px" append-to-body>
      <div class="quick">
        <el-tag v-for="r in roots" :key="r.path" class="tag" effect="plain" @click="load(r.path)">
          {{ r.name }}
        </el-tag>
      </div>
      <div class="current">
        <el-button :disabled="!listing?.parent" size="small" @click="goUp">上一级</el-button>
        <span class="path">{{ current }}</span>
        <el-tag v-if="listing && !listing.writable" type="warning" size="small">不可写</el-tag>
      </div>
      <div v-loading="loading" class="list">
        <div v-if="listing && !listing.entries.length" class="empty">（没有子目录）</div>
        <div v-for="e in listing?.entries ?? []" :key="e.path" class="item" @click="enter(e.path)">
          {{ e.name }}
        </div>
        <div v-if="listing?.truncated" class="empty">（条目过多，已截断）</div>
      </div>
      <template #footer>
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" @click="choose">选择此目录</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.dirpicker {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
}
.dirpicker :deep(.el-input) {
  flex: 1;
}
.btn {
  flex-shrink: 0;
}
.quick {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}
.tag {
  cursor: pointer;
}
.current {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.path {
  flex: 1;
  overflow: hidden;
  font-size: 12px;
  color: var(--text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.list {
  height: 280px;
  overflow: auto;
  border: 1px solid var(--border, #2b3244);
  border-radius: 6px;
}
.item {
  padding: 6px 10px;
  font-size: 13px;
  cursor: pointer;
}
.item:hover {
  background: var(--hover, rgba(127, 127, 127, 0.14));
}
.empty {
  padding: 12px;
  font-size: 12px;
  color: var(--text-muted);
}
</style>
