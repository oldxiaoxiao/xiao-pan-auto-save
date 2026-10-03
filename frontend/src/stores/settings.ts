import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "../api/client";
import type { Settings, SettingKey, SchedulerInfo } from "../api/types";

/** 全局设置 store：拉取 + 分键保存 + 调度器状态。 */
export const useSettingsStore = defineStore("settings", () => {
  const settings = ref<Settings>({
    crontab: "0 9 * * *",
    push_config: { CONSOLE: true },
    magic_regex: {},
    source: {},
    notify_enabled: true,
    sign_enabled: true,
    download: {
      mode: "builtin",
      dir: "",
      concurrency: 2,
      aria2: { host_port: "", secret: "", pause: false },
      emby: { url: "", token: "" },
    },
  });
  const scheduler = ref<SchedulerInfo>({ next_run: null, trigger: null });
  const loading = ref(false);
  const error = ref("");

  async function load() {
    loading.value = true;
    error.value = "";
    try {
      settings.value = await api.getSettings();
      scheduler.value = await api.scheduler();
    } catch (e) {
      error.value = (e as Error).message;
    } finally {
      loading.value = false;
    }
  }

  async function save(key: SettingKey, value: unknown) {
    const resp = await api.putSetting(key, value);
    // 后端回传规范化后的值，避免本地漂移
    (settings.value as Record<string, unknown>)[key] = resp.value;
    if (key === "crontab") scheduler.value = await api.scheduler();
    return resp.value;
  }

  return { settings, scheduler, loading, error, load, save };
});
