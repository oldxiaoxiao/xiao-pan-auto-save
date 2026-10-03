<script setup lang="ts">
import { ref, onMounted } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api/client";
import type { DriverInfo } from "../api/types";

const CAP_LABELS: Record<string, string> = {
  rename: "重命名",
  delete: "删除",
  move: "移动",
  mkdir: "建目录",
  sign: "签到",
  recycle: "回收站",
  account: "账号信息",
  preview: "分享预览",
};

const drivers = ref<DriverInfo[]>([]);
const loading = ref(false);

async function load() {
  loading.value = true;
  try {
    drivers.value = await api.drivers();
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div v-loading="loading" class="pane">
    <h3>驱动管理</h3>
    <p class="text-muted">网盘驱动能力矩阵。仅已支持的驱动可执行浏览、转存与签到。</p>
    <div class="table">
      <div class="tr th"><span>驱动</span><span>状态</span><span>能力</span><span>分享域名</span></div>
      <div v-for="d in drivers" :key="d.key" class="tr">
        <span class="name">{{ d.name }}</span>
        <span>
          <span class="badge" :class="d.supported ? 'badge--success' : 'badge--muted'">
            {{ d.supported ? "已支持" : "即将支持" }}
          </span>
        </span>
        <span class="caps">
          <span v-for="c in d.capability" :key="c" class="chip">{{ CAP_LABELS[c] || c }}</span>
        </span>
        <span class="mono text-muted domains">{{ d.share_domains.join(", ") || "—" }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pane h3 {
  margin: 0 0 6px;
}
.table {
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  background: #fff;
}
.tr {
  display: grid;
  grid-template-columns: 120px 96px 1fr 1.2fr;
  gap: 10px;
  padding: 10px 14px;
  align-items: center;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}
.tr:last-child {
  border-bottom: none;
}
.th {
  background: #f7f8fa;
  color: var(--text-muted);
  font-weight: 600;
}
.name {
  font-weight: 600;
}
.caps {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.domains {
  font-size: 12px;
  word-break: break-all;
}
</style>
