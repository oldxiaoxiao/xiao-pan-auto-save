import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { api } from "../api/client";
import type { Settings, SettingKey, SchedulerInfo } from "../api/types";

/** 全局设置 store：拉取 + 分键保存 + 调度器状态。 */
export const useSettingsStore = defineStore("settings", () => {
  // "还没 GET 到 /api/settings" 是一种真实存在的状态，就用类型里存在的状态表达：null。
  // 原先那份 placeholder 只在 task_defaults 上少一个键，再用 `as Settings` 断言回完整类型，
  // 于是"这个键现在还没有"从编译器眼里彻底消失，消费点只剩运行时兜底；
  // 后面还会再加新建路径（Task 4 的复制入口），没有编译器把关就会漏。
  // 前端也不在这里抄一份默认值兜底——task_defaults 只住后端那一份。
  const settings = ref<Settings | null>(null);
  /** 新建任务所需的那份真默认值到手了没。新建路径必须先过这道门。 */
  const ready = computed(() => !!settings.value?.task_defaults);
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
    // 后端回传规范化后的值，避免本地漂移。
    // 本地副本也可能是 null（进设置页之前那次 GET 就失败了）：值已经在服务端落库，
    // 这种情况直接回读一次拿权威值，既不往 null 上写属性，也不在本地猜一份。
    if (!settings.value) {
      await load();
    } else {
      // 这里的断言只覆盖一处信任边界：PUT /api/settings/{key} 回传的 value 就是这个键的新值
      // （类型上是 unknown）。它和原先那个"把未载入伪装成已就绪"的 `as Settings` 不是一回事——
      // 没 GET 到就是 null，编译器照样管着。
      settings.value = { ...settings.value, [key]: resp.value } as Settings;
    }
    if (key === "crontab") scheduler.value = await api.scheduler();
    return resp.value;
  }

  return { settings, ready, scheduler, loading, error, load, save };
});
