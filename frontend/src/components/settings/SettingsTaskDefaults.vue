<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { storeToRefs } from "pinia";
import { useSettingsStore } from "../../stores/settings";
import { QUALITY_OPTIONS, RUN_MODE_OPTIONS } from "../../constants";
import type { TaskDefaults } from "../../api/types";

const store = useSettingsStore();
const { settings } = storeToRefs(store);

// settings 是 Settings | null：null 就是「GET 还没成功」，这里不前端猜一份 task_defaults 顶上，
// 草稿只在服务端那份真值落地后由下面的 watch 填入（SettingsCron 同款口径：服务端值 + 本地草稿）。
const draft = ref<TaskDefaults | null>(null);
let serverSnapshot = "";
watch(
  () => settings.value?.task_defaults,
  (v) => {
    if (!v) return; // 还没拉到：留着 null 渲染"读取中"，别把空位当值
    const snap = JSON.stringify(v);
    if (snap === serverSnapshot) return; // 服务端值没再变，就不盖掉用户正在编辑的草稿
    serverSnapshot = snap;
    draft.value = { ...v };
  },
  { immediate: true },
);

// draft.quality 以逗号分隔存储；UI 用数组双向映射（与 TaskForm 里同名 computed 同一口径）。
const qualityList = computed<string[]>({
  get: () => (draft.value?.quality ? draft.value.quality.split(",").filter(Boolean) : []),
  set: (v) => {
    if (draft.value) draft.value.quality = v.join(",");
  },
});

/** 抓取范围的两档真状态，与任务表单首屏同一个值域（"" = 全部文件、"$TV" = 只抓剧集）。
 *  档位语义只认这两个值；任务若带着自定义正则，这里不假装选中任何一档，也不代为改写。 */
const PATTERN_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "全部文件" },
  { value: "$TV", label: "只抓剧集" },
];

async function save() {
  if (!draft.value) {
    // 六个键一个都没到手，此时保存只会把前端空位写进设置——挡下。
    ElMessage.warning("设置还没读取到，请稍后重试");
    return;
  }
  const d = draft.value;
  try {
    // 六个键一次交齐：后端允许部分写入（缺的键回落出厂默认），但面板不靠那个回落去省字段。
    await store.save("task_defaults", {
      savepath_root: d.savepath_root,
      auto_download: d.auto_download,
      run_mode: d.run_mode,
      pattern: d.pattern,
      quality: d.quality,
      subdir_filter: d.subdir_filter,
    });
    ElMessage.success("已保存新建默认");
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}
</script>

<template>
  <div class="pane">
    <h3>新建任务默认</h3>
    <p v-if="!draft">
      <span>正在读取设置…（读到之前不显示默认值，免得把没到手的值当成服务端说过的话）</span>
    </p>
    <template v-else>
      <div class="row">
        <label>保存路径根</label>
        <el-input v-model="draft.savepath_root" />
      </div>
      <div class="row">
        <label>下载到本地</label>
        <el-switch v-model="draft.auto_download" />
      </div>
      <div class="row">
        <label>执行方式</label>
        <el-select v-model="draft.run_mode">
          <el-option
            v-for="option in RUN_MODE_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
      </div>
      <div class="row">
        <label>抓取范围</label>
        <el-select v-model="draft.pattern">
          <el-option
            v-for="option in PATTERN_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
      </div>
      <div class="row">
        <label>画质</label>
        <el-select
          v-model="qualityList"
          multiple
          clearable
          placeholder="留空 = 不限画质；勾选若干画质时，新建任务默认只转命中这些画质档的文件"
        >
          <el-option
            v-for="qualityOption in QUALITY_OPTIONS"
            :key="qualityOption"
            :label="qualityOption"
            :value="qualityOption"
          />
        </el-select>
      </div>
      <div class="row">
        <label>子目录也按集数/画质过滤</label>
        <el-switch v-model="draft.subdir_filter" />
      </div>
      <p class="foot">
        <span>只影响以后新建的任务，不会改动已有任务</span>
      </p>
      <el-button @click="save">
        <span>保存</span>
      </el-button>
    </template>
  </div>
</template>

<style scoped>
.pane {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 640px;
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
  width: 176px;
  flex: none;
  font-size: 13px;
  color: #5b6270;
}
.row .el-input,
.row .el-select {
  flex: 1;
}
.foot {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
}
</style>
