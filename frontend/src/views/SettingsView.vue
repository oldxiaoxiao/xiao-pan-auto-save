<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { ElMessage } from "element-plus";
import { storeToRefs } from "pinia";
import { useSettingsStore } from "../stores/settings";
import type { Settings } from "../api/types";
import SettingsCron from "../components/settings/SettingsCron.vue";
import SettingsNotify from "../components/SettingsNotify.vue";
import SettingsMagic from "../components/SettingsMagic.vue";
import SettingsSource from "../components/SettingsSource.vue";
import SettingsDownload from "../components/settings/SettingsDownload.vue";
import SettingsApi from "../components/SettingsApi.vue";
import SettingsDrivers from "../components/SettingsDrivers.vue";

const store = useSettingsStore();
const { loading, error } = storeToRefs(store);
const active = ref("cron");

// 这里刻意保留一份**显式**的空值边界：本视图和它下挂的各个面板都建立在"GET /api/settings 已经成功"
// 的假设上（每个面板自己 watch 服务端值，值一到就同步），所以 null 只覆盖"还没拉到"那一小段窗口：
// 读出来是 undefined，开关显示为关、输入框留空，不冒充服务端说过的值，也不必每个面板各写一遍判空。
const view = computed<Partial<Settings>>(() => store.settings ?? {});

const groups = [
  { key: "cron", label: "定时规则", comp: SettingsCron },
  { key: "notify", label: "通知渠道", comp: SettingsNotify },
  { key: "magic", label: "魔法匹配", comp: SettingsMagic },
  { key: "source", label: "资源搜索源", comp: SettingsSource },
  { key: "download", label: "下载设置", comp: SettingsDownload },
  { key: "api", label: "API", comp: SettingsApi },
  { key: "drivers", label: "驱动管理", comp: SettingsDrivers },
];

async function toggle(key: "notify_enabled" | "sign_enabled", val: boolean) {
  try {
    await store.save(key, val);
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

onMounted(() => store.load());
</script>

<template>
  <div>
    <div class="sticky-bar">
      <span class="sticky-bar__title">设置</span>
      <div class="globals">
        <label>运行通知</label>
        <el-switch
          :model-value="view.notify_enabled === true"
          size="small"
          @change="(v: string | number | boolean) => toggle('notify_enabled', !!v)"
        />
        <label>自动签到</label>
        <el-switch
          :model-value="view.sign_enabled === true"
          size="small"
          @change="(v: string | number | boolean) => toggle('sign_enabled', !!v)"
        />
      </div>
    </div>

    <p v-if="error" class="err">
      {{ error }}
    </p>

    <div v-else v-loading="loading" class="wrap">
      <nav class="subnav">
        <button
          v-for="g in groups"
          :key="g.key"
          class="subnav__item"
          :class="{ on: active === g.key }"
          @click="active = g.key"
        >
          {{ g.label }}
        </button>
      </nav>

      <section class="body card">
        <component :is="g.comp" v-for="g in groups" v-show="active === g.key" :key="g.key" />
      </section>
    </div>
  </div>
</template>

<style scoped>
.globals {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-muted);
}
.globals label {
  margin-left: 6px;
}
.wrap {
  display: grid;
  grid-template-columns: 160px 1fr;
  gap: 16px;
  align-items: start;
}
.subnav {
  display: flex;
  flex-direction: column;
  gap: 4px;
  position: sticky;
  top: 72px;
}
.subnav__item {
  text-align: left;
  border: none;
  background: transparent;
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 14px;
  color: #5b6270;
}
.subnav__item:hover {
  background: #f2f4f7;
}
.subnav__item.on {
  background: var(--primary-soft);
  color: var(--primary);
  font-weight: 600;
}
.body {
  min-height: 320px;
}
.pane h3 {
  margin: 0 0 8px;
}
.placeholder {
  margin-top: 16px;
  padding: 40px;
  text-align: center;
  color: var(--text-muted);
  border: 1px dashed var(--border);
  border-radius: 10px;
}
.err {
  color: var(--danger);
}
code {
  background: #f0f2f5;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 12px;
}
@media (max-width: 767px) {
  .wrap {
    grid-template-columns: 1fr;
  }
  .subnav {
    flex-direction: row;
    overflow-x: auto;
    position: static;
  }
  .subnav__item {
    white-space: nowrap;
  }
}
</style>
