<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../../api/client";
import type { AutostartState, UpdateCheck } from "../../api/types";

const state = ref<AutostartState | null>(null);
const busy = ref(false);
const msg = ref("");

/* ---------- 版本 ---------- */
const update = ref<UpdateCheck | null>(null);
const upLoading = ref(false);
const upMsg = ref("");
const backupBusy = ref(false);

async function checkUpdate(force = false) {
  upLoading.value = true;
  upMsg.value = "";
  try {
    update.value = (await api.updateCheck(force)).data;
  } catch (e) {
    // 页面加载时的那次静默检查也不该弹窗打扰：失败只留一行说明
    upMsg.value = `检查更新失败：${(e as Error).message}`;
  } finally {
    upLoading.value = false;
  }
}

const primary = computed(() => update.value?.assets?.[0] ?? null);

function variantLabel(v: string): string {
  return v === "full" ? "完整版（自带 aria2）" : "精简版";
}

function fmtSize(n: number): string {
  return n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`;
}

/** 各平台的原生校验命令：ZIP 用户手上没有源码，给 python 脚本等于没给。 */
function verifyCmd(name: string): string {
  const p = update.value?.platform ?? "";
  if (p.startsWith("windows")) return `certutil -hashfile ${name} SHA256`;
  if (p.startsWith("macos")) return `shasum -a 256 ${name}`;
  return `sha256sum ${name}`;
}

async function copyCmd() {
  const cmd = primary.value ? verifyCmd(primary.value.name) : "";
  if (!cmd) return;
  try {
    await navigator.clipboard.writeText(cmd);
    ElMessage.success("已复制校验命令");
  } catch {
    ElMessage.info("浏览器拒绝了剪贴板访问，请手动复制");
  }
}

async function backupNow() {
  backupBusy.value = true;
  try {
    const r = await api.updatePrebackup();
    ElMessage.success(r.message);
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    backupBusy.value = false;
  }
}

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

// 进页面顺手查一次（后端有 6 小时缓存，失败静默）；想立刻知道结果就点「检查更新」
onMounted(() => {
  load();
  void checkUpdate();
});
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

    <div class="block">
      <div class="row">
        <label>版本</label>
        <span class="ver">{{ update?.current || "-" }}</span>
        <el-button size="small" :loading="upLoading" @click="checkUpdate(true)"> 检查更新 </el-button>
        <el-button v-if="update?.has_update" size="small" :loading="backupBusy" @click="backupNow">
          升级前备份
        </el-button>
      </div>

      <p v-if="update?.has_update" class="hint">
        有新版本 <b>{{ update.latest }}</b>
        <template v-if="primary">
          ：
          <el-link v-for="a in update.assets" :key="a.url" :href="a.url" target="_blank" type="primary">
            {{ variantLabel(a.variant) }}（{{ fmtSize(a.size) }}）
          </el-link>
        </template>
        <template v-else> ——当前平台没有对应的桌面包（{{ update.platform }}）。Compose 部署请更换镜像 tag。 </template>
      </p>
      <p v-else-if="update?.latest" class="text-muted">已是最新（{{ update.latest }}）</p>

      <!-- 下载完先校验再解压：校验不过的包一律不装 -->
      <div v-if="update?.has_update && primary" class="cmd">
        <code>{{ verifyCmd(primary.name) }}</code>
        <el-button size="small" link @click="copyCmd">复制</el-button>
        <span class="text-muted">
          与发布页 {{ update.sums ? update.sums.name : "SHA256SUMS" }} 里那一行比对，一致再解压替换
        </span>
      </div>

      <p v-if="update?.reason" class="text-muted">{{ update.reason }}</p>
      <p v-if="upMsg" class="text-muted">{{ upMsg }}</p>
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
.block {
  border-top: 1px solid var(--border, #e6e8ee);
  padding-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ver {
  font-size: 13px;
}
.hint {
  margin: 0;
  font-size: 13px;
}
.hint .el-link {
  margin-left: 8px;
  font-size: 13px;
}
.cmd {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.cmd code {
  font-size: 12px;
  padding: 4px 8px;
  border-radius: 4px;
  background: var(--hover, rgba(127, 127, 127, 0.12));
}
</style>
