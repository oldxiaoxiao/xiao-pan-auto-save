<script setup lang="ts">
import { computed } from "vue";
import type { Task } from "../api/types";
import { relativeTime, weekText } from "../utils";

const props = defineProps<{ task: Task; index: number; dirty: boolean; expanded: boolean }>();
const emit = defineEmits<{
  (e: "toggle"): void;
  (e: "run", id: number): void;
  (e: "position", where: "top" | "bottom"): void;
}>();

function onPosition(where: "top" | "bottom") {
  emit("position", where);
}

const WEEK_CN = ["日", "一", "二", "三", "四", "五", "六"];

/** 更新频率口语化：本项目只有 interval:分钟 与 cron:<标准 5 段> 两种写法，够用了。
 *  每天/每周X HH:MM、每 N 分钟；认不出来的写法（含非法值，后端会回退全局 crontab）返回空串，
 *  由调用方显示「继承全局」。刻意不引 cron 解析库。 */
function humanFrequency(schedule: string): string {
  const raw = (schedule || "").trim();
  const interval = /^interval:(\d+)$/i.exec(raw);
  if (interval) return Number(interval[1]) > 0 ? `每 ${Number(interval[1])} 分钟` : "";
  const body = /^cron:(.+)$/i.exec(raw)?.[1]?.trim();
  const fields = (body || "").split(/\s+/);
  if (fields.length !== 5) return "";
  const [minute, hour, dom, month, dow] = fields;
  // 只口语化「分/时 + 每月每天」这一族；带日期区间或月份限定的交给原始值，别硬编
  if (dom !== "*" || month !== "*" || !/^\d{1,2}$/.test(minute) || !/^\d{1,2}$/.test(hour)) return "";
  if (Number(minute) > 59 || Number(hour) > 23) return "";
  const time = `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
  if (dow === "*") return `每天 ${time}`;
  if (!/^\d(?:,\d){0,6}$/.test(dow)) return "";
  const nums = dow.split(",").map((d) => Number(d));
  if (nums.some((d) => d > 7)) return ""; // cron 的周是 0-7（0 与 7 都是周日），越界当作认不出
  const days = [...new Set(nums.map((d) => d % 7))];
  return `每周${days.map((d) => WEEK_CN[d]).join("、")} ${time}`;
}

const chips = computed(() => {
  const t = props.task;
  const list: { key: string; text: string; primary?: boolean; success?: boolean }[] = [];
  // 执行方式徽标排在形态细节之前；「已完成」由 once + 已停用派生（无法区分停用来源，已知瑕疵）
  if (t.run_mode === "manual") list.push({ key: "m", text: "仅手动" });
  if (t.run_mode === "once" && !t.disabled) list.push({ key: "o", text: "一次性待执行", primary: true });
  if (t.run_mode === "once" && t.disabled) list.push({ key: "done", text: "已完成", success: true });
  if (t.run_mode === "follow" && t.schedule) {
    const freq = humanFrequency(t.schedule);
    // 非法/自定义到认不出的频率：后端确实回退了全局 crontab，这里就说实话
    list.push({ key: "s", text: freq ? `频率 ${freq}` : "继承全局" });
  }
  if (t.pattern) list.push({ key: "p", text: `正则 ${t.pattern}`, primary: true });
  if (t.replace) list.push({ key: "r", text: `替换 ${t.replace}` });
  if (t.ignore_extension) list.push({ key: "e", text: "忽略扩展名" });
  if (t.update_subdir) {
    list.push({ key: "u", text: `子目录追更${t.update_subdir_resave ? "[重存]" : "[递归]"}` });
  }
  if (t.startfid) list.push({ key: "sf", text: "已选起始文件" });
  if (t.auto_download) list.push({ key: "dl", text: `本地下载${t.download_subdir ? "[含子目录]" : ""}` });
  if (t.enddate) list.push({ key: "d", text: `截止 ${t.enddate}` });
  if (t.runweek && t.runweek.length && t.runweek.length < 7) list.push({ key: "w", text: weekText(t.runweek) });
  return list;
});

const lastRun = computed(() => relativeTime(props.task.last_run_at));

/** 一次性任务停用即「已完成」：此时不再显示灰「停用」，改由绿徽标说明状态。 */
const isOnceDone = computed(() => props.task.run_mode === "once" && props.task.disabled);
</script>

<template>
  <div class="row card" :class="{ expanded }" @click="emit('toggle')">
    <div class="idx">
      {{ index + 1 }}
    </div>
    <div class="main">
      <div class="title-line">
        <span class="name">{{ task.taskname }}</span>
        <span v-if="dirty" class="dot-unsaved" title="未保存" />
        <span v-if="task.shareurl_ban" class="badge badge--danger">失效</span>
        <span v-if="task.disabled && !isOnceDone" class="badge badge--muted">停用</span>
        <span v-else-if="lastRun" class="badge badge--muted">{{ lastRun }}</span>
      </div>
      <div class="path text-muted mono">→ {{ task.savepath }}</div>
      <div class="url text-muted mono">
        {{ task.shareurl }}
      </div>
      <div class="chips">
        <span
          v-for="c in chips"
          :key="c.key"
          class="chip"
          :class="{ 'is-primary': c.primary, 'is-success': c.success }"
          >{{ c.text }}</span
        >
      </div>
    </div>
    <div class="ops" @click.stop>
      <el-button size="small" text @click="emit('run', task.id)"> ▶ 运行 </el-button>
      <el-dropdown trigger="click" @command="onPosition">
        <el-button size="small" text title="排序"> ⋮ </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="top">置顶</el-dropdown-item>
            <el-dropdown-item command="bottom">置底</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <span class="caret">{{ expanded ? "▲" : "▼" }}</span>
    </div>
  </div>
</template>

<style scoped>
.row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  cursor: pointer;
  transition: box-shadow 0.15s;
}
.row:hover {
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.05);
}
.row.expanded {
  border-color: var(--primary);
}
.idx {
  width: 22px;
  color: var(--text-muted);
  font-size: 13px;
  text-align: right;
  padding-top: 2px;
}
.main {
  flex: 1;
  min-width: 0;
}
.title-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.name {
  font-weight: 600;
  font-size: 15px;
}
.path {
  font-size: 12.5px;
  margin-top: 3px;
}
.url {
  font-size: 12px;
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.ops {
  display: flex;
  align-items: center;
  gap: 4px;
}
.caret {
  color: var(--text-muted);
  font-size: 12px;
}
</style>
