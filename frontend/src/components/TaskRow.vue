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

const chips = computed(() => {
  const t = props.task;
  const list: { key: string; text: string; primary?: boolean }[] = [];
  if (t.pattern) list.push({ key: "p", text: `正则 ${t.pattern}`, primary: true });
  if (t.replace) list.push({ key: "r", text: `替换 ${t.replace}` });
  if (t.ignore_extension) list.push({ key: "e", text: "忽略扩展名" });
  if (t.update_subdir) {
    list.push({ key: "u", text: `子目录追更${t.update_subdir_resave ? "[重存]" : "[递归]"}` });
  }
  if (t.startfid) list.push({ key: "s", text: "已选起始文件" });
  if (t.auto_download) list.push({ key: "dl", text: `本地下载${t.download_subdir ? "[含子目录]" : ""}` });
  if (t.enddate) list.push({ key: "d", text: `截止 ${t.enddate}` });
  if (t.runweek && t.runweek.length && t.runweek.length < 7) list.push({ key: "w", text: weekText(t.runweek) });
  return list;
});

const lastRun = computed(() => relativeTime(props.task.last_run_at));
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
        <span v-if="task.disabled" class="badge badge--muted">停用</span>
        <span v-else-if="lastRun" class="badge badge--muted">{{ lastRun }}</span>
      </div>
      <div class="path text-muted mono">→ {{ task.savepath }}</div>
      <div class="url text-muted mono">
        {{ task.shareurl }}
      </div>
      <div class="chips">
        <span v-for="c in chips" :key="c.key" class="chip" :class="{ 'is-primary': c.primary }">{{ c.text }}</span>
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
.more-btn {
  color: var(--text-muted);
}
</style>
