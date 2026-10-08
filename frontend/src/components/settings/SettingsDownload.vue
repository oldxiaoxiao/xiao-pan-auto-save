<script setup lang="ts">
import { reactive, watch } from "vue";
import { ElMessage } from "element-plus";
import { storeToRefs } from "pinia";
import { useSettingsStore } from "../../stores/settings";
import type { DownloadSettings } from "../../api/types";

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
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}
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
    <div class="row">
      <label>下载根目录</label>
      <el-input v-model="draft.dir" placeholder="留空 = 当前数据目录下的 downloads；也可填写绝对路径" />
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
</style>
