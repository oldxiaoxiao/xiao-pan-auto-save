<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { storeToRefs } from "pinia";
import { useSettingsStore } from "../../stores/settings";
import { api } from "../../api/client";
import DirPicker from "../DirPicker.vue";
import type { DownloadEngine, DownloadSettings } from "../../api/types";

const store = useSettingsStore();
const { settings } = storeToRefs(store);

const draft = reactive<DownloadSettings>({
  mode: "builtin",
  dir: "",
  concurrency: 2,
  history_retention: "days_90",
  aria2: { host_port: "", secret: "", pause: false },
  emby: { url: "", token: "" },
});
let loaded = "";

watch(
  // settings 是 Settings | null：下面 `if (!v)` 就是为"还没拉到"这一窗准备的，值一到自动填。
  () => settings.value?.download,
  (v) => {
    if (!v || loaded === JSON.stringify(v)) return;
    Object.assign(draft, { ...v, aria2: { ...v.aria2 }, emby: { ...v.emby } });
    loaded = JSON.stringify(v);
  },
  { immediate: true, deep: true },
);

async function save() {
  try {
    await store.save("download", { ...draft });
    ElMessage.success("下载设置已保存");
    await refreshEngine();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

// ---- 引擎状态：让用户看清"到底用的 aria2 还是内置"，降级不再无声 ----
const engine = ref<DownloadEngine | null>(null);
const busy = ref<"" | "start" | "install">("");
const engineMsg = ref("");

const SOURCE_TEXT: Record<string, string> = {
  bundled: "随包自带",
  managed: "一键安装",
  system: "系统 PATH",
  path: "常见安装路径",
};
const sourceText = computed(() =>
  engine.value?.source ? (SOURCE_TEXT[engine.value.source] ?? engine.value.source) : "",
);

async function refreshEngine() {
  try {
    engine.value = (await api.downloadEngine()).data;
  } catch {
    engine.value = null;
  }
}

async function startAria2() {
  busy.value = "start";
  engineMsg.value = "";
  try {
    engineMsg.value = (await api.aria2Start()).message;
  } catch (e) {
    engineMsg.value = (e as Error).message;
  } finally {
    busy.value = "";
    await refreshEngine();
  }
}

async function installAria2() {
  busy.value = "install";
  engineMsg.value = "";
  try {
    engineMsg.value = (await api.aria2Install()).message;
  } catch (e) {
    engineMsg.value = (e as Error).message;
  } finally {
    busy.value = "";
    await refreshEngine();
  }
}

onMounted(refreshEngine);
</script>

<template>
  <div class="pane">
    <h3>下载到本地</h3>
    <p class="text-muted">任务开启「下载到本地」后，转存成功的新文件将按以下设置落盘。</p>

    <div class="row">
      <label>模式</label>
      <el-radio-group v-model="draft.mode">
        <el-radio-button value="builtin">内置下载器</el-radio-button>
        <el-radio-button value="aria2">Aria2 RPC</el-radio-button>
      </el-radio-group>
    </div>

    <div class="row engine-row">
      <label>引擎状态</label>
      <div class="engine">
        <el-tag :type="engine?.running ? 'success' : 'info'" size="small">
          {{ engine?.running ? "aria2 已运行" : "aria2 未运行" }}
        </el-tag>
        <el-tag v-if="engine?.engine" :type="engine.engine === 'aria2' ? 'success' : 'warning'" size="small">
          上次实际使用：{{ engine.engine === "aria2" ? "aria2" : "内置下载器" }}
        </el-tag>
        <span class="text-muted">
          {{ engine?.installed ? `已检测到 aria2c（${sourceText}）` : "未检测到 aria2c" }} · RPC
          {{ engine?.host_port || "127.0.0.1:6800" }}
        </span>
        <div class="engine-actions">
          <el-button size="small" :loading="busy === 'start'" @click="startAria2">启动 aria2</el-button>
          <el-button v-if="!engine?.installed" size="small" :loading="busy === 'install'" @click="installAria2">
            一键安装 aria2c
          </el-button>
          <el-button size="small" @click="refreshEngine">刷新</el-button>
        </div>
      </div>
    </div>
    <p v-if="engineMsg" class="text-muted engine-msg">{{ engineMsg }}</p>
    <p v-if="engine?.detail" class="text-muted engine-msg">上次：{{ engine.detail }}</p>
    <p class="text-muted engine-msg">
      选 aria2 模式后，下载时会自动拉起 aria2（未填地址则用 127.0.0.1:6800）；起不来会明确提示，并临时改用内置下载器。
    </p>
    <div class="row">
      <label>下载根目录</label>
      <DirPicker v-model="draft.dir" placeholder="留空 = 当前数据目录下的 downloads；也可填写绝对路径" />
    </div>
    <div class="row">
      <label>并发数</label>
      <el-input-number v-model="draft.concurrency" :min="1" :max="8" />
      <span class="text-muted">仅内置下载器生效</span>
    </div>
    <div class="row">
      <label>历史保留</label>
      <el-select v-model="draft.history_retention" style="width: 200px">
        <el-option label="最近 30 天" value="days_30" />
        <el-option label="最近 90 天" value="days_90" />
        <el-option label="最近 180 天" value="days_180" />
        <el-option label="永久保留（手动清理）" value="forever" />
      </el-select>
      <span class="text-muted">自动清理过期下载记录，永久保留则只靠历史页的手动清理</span>
    </div>

    <template v-if="draft.mode === 'aria2'">
      <div class="row">
        <label>RPC 地址</label>
        <el-input v-model="draft.aria2.host_port" placeholder="127.0.0.1:6800 或 http://host:6800" />
      </div>
      <div class="row">
        <label>RPC 密钥</label>
        <el-input v-model="draft.aria2.secret" placeholder="aria2-rpc-secret（可空）" />
      </div>
      <div class="row">
        <label>添加后暂停</label>
        <el-switch v-model="draft.aria2.pause" />
        <span class="text-muted">开启后需手动/由 aria2 前端开始下载</span>
      </div>
    </template>

    <div class="row">
      <label>Emby 地址</label>
      <el-input v-model="draft.emby.url" placeholder="http://192.168.1.10:8096（可空，下载成功后刷新媒体库）" />
    </div>
    <div class="row">
      <label>Emby API Key</label>
      <el-input v-model="draft.emby.token" placeholder="X-Emby-Token（可空）" />
    </div>

    <el-button type="primary" @click="save"> 保存 </el-button>
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
.row .el-input {
  flex: 1;
}
.engine-row {
  align-items: flex-start;
}
.engine {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  flex: 1;
}
.engine-actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
}
.engine-msg {
  font-size: 12px;
  margin: -4px 0 0;
}
</style>
