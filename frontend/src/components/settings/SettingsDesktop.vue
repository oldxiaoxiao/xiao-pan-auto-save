<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../../api/client";
import type { AutostartState } from "../../api/types";

const state = ref<AutostartState | null>(null);
const busy = ref(false);
const msg = ref("");

const METHOD_LABEL: Record<string, string> = {
  windows: "Windows 启动目录",
  macos: "macOS 登录项",
  linux: "Linux autostart",
};
const methodLabel = computed(() =>
  state.value?.method ? (METHOD_LABEL[state.value.method] ?? state.value.method) : "",
);

async function load() {
  try {
    state.value = (await api.desktopAutostart()).data;
  } catch (e) {
    msg.value = (e as Error).message;
  }
}

async function toggleAutostart(next: boolean) {
  if (!state.value?.supported) return;
  busy.value = true;
  msg.value = "";
  try {
    const r = await api.setDesktopAutostart(next);
    msg.value = r.message;
    if (r.ok && state.value) state.value = { ...state.value, enabled: r.enabled };
    else ElMessage.error(r.message);
  } catch (e) {
    msg.value = (e as Error).message;
    ElMessage.error((e as Error).message);
  } finally {
    busy.value = false;
  }
}

async function testNotify() {
  busy.value = true;
  msg.value = "";
  try {
    const r = await api.desktopNotify("小盘自动转存", "这是一条测试通知，来自「设置 → 客户端」。");
    msg.value = r.message;
    if (!r.ok) ElMessage.error(r.message);
  } catch (e) {
    msg.value = (e as Error).message;
    ElMessage.error((e as Error).message);
  } finally {
    busy.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="pane">
    <h3>客户端</h3>
    <p class="text-muted">
      这里的设置只对 <b>Windows / macOS 桌面客户端</b> 生效。以 Compose 或服务端方式运行时没有
      "这台电脑的登录项"，也没有当前登录用户的桌面通知，此时开关会如实禁用并说明原因。
    </p>

    <div class="row">
      <label>开机自启</label>
      <el-switch :model-value="!!state?.enabled" :disabled="!state?.supported || busy" @change="toggleAutostart" />
      <span v-if="!state?.supported" class="text-muted">{{ state?.detail || "当前环境不支持" }}</span>
      <span v-else class="text-muted">登录系统后自动启动客户端（{{ methodLabel }}）</span>
    </div>

    <div class="row">
      <label>桌面通知</label>
      <el-button size="small" :disabled="busy" @click="testNotify"> 发送测试通知 </el-button>
      <span class="text-muted"> 走系统通知机制（macOS 通知中心 / Windows 托盘气泡 / Linux notify-send） </span>
    </div>

    <p v-if="msg" class="text-muted">{{ msg }}</p>
  </div>
</template>

<style scoped>
.pane {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 680px;
}
h3 {
  margin: 0;
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.row label {
  width: 92px;
  flex: none;
  font-size: 13px;
  color: #5b6270;
}
</style>
