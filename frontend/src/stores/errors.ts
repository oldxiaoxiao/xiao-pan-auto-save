import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";

export interface FrontendError {
  at: string;
  where: string;
  text: string;
}

/**
 * 界面错误登记处。
 *
 * 存在的理由只有一个：打包后的客户端**没有控制台**。Vue 默认把渲染错误只打到控制台，
 * 于是一个组件渲染失败在用户那里就变成"从此点什么都没反应"，既没有提示也没有线索。
 * 这里把错误攒下来并显示在界面上，用户能直接复制原文——排查"点了没反应"这类问题，
 * 这是唯一可靠的抓手（不同平台的 WebView 行为差得多，开发环境未必复现得出来）。
 */
export const useErrorsStore = defineStore("errors", () => {
  const items = ref<FrontendError[]>([]);

  /** 最多留 20 条：够定位，又不至于无限增长。 */
  function report(err: unknown, where: string): void {
    const text = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
    items.value.push({ at: new Date().toISOString(), where, text });
    if (items.value.length > 20) items.value.shift();
    console.error(`[xiao-pan] ${where}`, err);
    ElMessage.error(`界面出错了：${text}`);
  }

  function clear(): void {
    items.value = [];
  }

  /** 供复制：带时间和出处，贴给别人时能照原样复述。 */
  const asText = computed(() => items.value.map((e) => `${e.at} · ${e.where}\n  ${e.text}`).join("\n") || "");

  return { items, report, clear, asText };
});
