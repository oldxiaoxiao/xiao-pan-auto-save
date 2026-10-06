<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { useSettingsStore } from "../../stores/settings";
import { CRON_PRESETS } from "../../constants";
import { formatTime } from "../../utils";

const store = useSettingsStore();
// settings 是 Settings | null：这里读到的空串只代表"还没拉到"，不前端猜一份 crontab 默认值；
// 值一落地由下面这条 watch 补进输入框（面板其他几块也是这个口径：服务端值 + 本地草稿）。
const crontab = ref(store.settings?.crontab ?? "");
watch(
  () => store.settings?.crontab,
  (v) => {
    if (v !== undefined) crontab.value = v;
  },
);

const nextRunText = computed(() =>
  store.scheduler.next_run ? formatTime(store.scheduler.next_run) : "（调度器未就绪）",
);

async function save() {
  if (!store.settings) {
    // 还没 GET 到就是空串窗口：这时候按保存，会把库里那条真 crontab 抹成 ""（后端只在调度时回落全局默认，
    // 存下来的仍是空）。与「新建默认」面板同一口径：没读到就挡下，让人稍后重试。
    ElMessage.warning("设置还没读取到，请稍后重试");
    return;
  }
  try {
    await store.save("crontab", crontab.value.trim());
    ElMessage.success("定时规则已保存");
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

function apply(v: string) {
  crontab.value = v;
}
</script>

<template>
  <div class="pane">
    <h3>定时规则</h3>
    <p class="text-muted">标准 5 段 crontab（分 时 日 月 周），保存后立即重新调度。</p>
    <el-input v-model="crontab" placeholder="0 9 * * *" class="mono" />
    <div class="presets">
      <button v-for="p in CRON_PRESETS" :key="p.value" class="preset" @click="apply(p.value)">
        {{ p.label }}
      </button>
    </div>
    <div class="next">
      <span class="text-muted">下次运行：</span>
      <b>{{ nextRunText }}</b>
      <span v-if="store.scheduler.trigger" class="chip mono">{{ store.scheduler.trigger }}</span>
    </div>
    <el-button type="primary" @click="save"> 保存 </el-button>
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
.presets {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.preset {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 6px;
  padding: 5px 10px;
  font-size: 13px;
  cursor: pointer;
}
.preset:hover {
  background: var(--primary-soft);
  color: var(--primary);
}
.next {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}
</style>
