<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { storeToRefs } from "pinia";
import { useTasksStore } from "../stores/tasks";
import { useAccountsStore } from "../stores/accounts";
import TaskRow from "../components/TaskRow.vue";
import TaskForm from "../components/TaskForm.vue";
import RunLogDialog from "../components/RunLogDialog.vue";
import type { Task, TaskPayload } from "../api/types";

const tasks = useTasksStore();
const accountsStore = useAccountsStore();
const { sorted, savePaths, dirty } = storeToRefs(tasks);
const { accounts } = storeToRefs(accountsStore);

const keyword = ref("");
const pathFilter = ref("");
const editingId = ref<number | "new" | null>(null);
const newDirty = ref(false);

const runDialog = ref(false);
const runTaskId = ref<number | null>(null);
const runName = ref("");
const pendingPrefill = ref<Partial<TaskPayload> | undefined>(undefined);

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  return sorted.value.filter((t) => {
    if (pathFilter.value && t.savepath !== pathFilter.value) return false;
    if (!kw) return true;
    return t.taskname.toLowerCase().includes(kw) || t.shareurl.toLowerCase().includes(kw);
  });
});

const canDrag = computed(() => !keyword.value.trim() && !pathFilter.value);
const hasUnsaved = computed(() => dirty.value.size > 0 || newDirty.value);

function toggleExpand(id: number) {
  editingId.value = editingId.value === id ? null : id;
}

function startNew() {
  pendingPrefill.value = undefined;
  editingId.value = "new";
  newDirty.value = false;
}

async function onSave(payload: TaskPayload) {
  try {
    if (editingId.value === "new") {
      const created = await tasks.createTask(payload);
      ElMessage.success("已创建任务");
      editingId.value = created.id;
      newDirty.value = false;
    } else if (typeof editingId.value === "number") {
      await tasks.updateTask(editingId.value, payload);
      ElMessage.success("已保存");
    }
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

function onDirty(v: boolean) {
  if (editingId.value === "new") newDirty.value = v;
  else if (typeof editingId.value === "number") tasks.markDirty(editingId.value, v);
}

async function onRemove(id: number) {
  const t = sorted.value.find((x) => x.id === id);
  try {
    await ElMessageBox.confirm(`确定删除任务《${t?.taskname ?? id}》？`, "删除确认", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    await tasks.deleteTask(id);
    editingId.value = null;
    ElMessage.success("已删除");
  } catch (e) {
    if (e !== "cancel" && e instanceof Error) ElMessage.error(e.message);
  }
}

function openRun(task: Task | null) {
  runTaskId.value = task ? task.id : null;
  runName.value = task ? task.taskname : "";
  runDialog.value = true;
}

async function importClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    const m = text.match(/https?:\/\/pan\.quark\.cn\/s\/\w+[^\s"'《》]*/);
    if (!m) {
      ElMessage.warning("剪贴板未找到夸克分享链接");
      return;
    }
    editingId.value = "new";
    newDirty.value = false;
    pendingPrefill.value = { shareurl: m[0] };
    ElMessage.success("已读取分享链接，请补全信息");
  } catch {
    ElMessage.error("无法读取剪贴板（需 https 或用户授权）");
  }
}

// —— 显式置顶 / 置底（不受搜索、筛选禁用拖拽的影响）——
async function onPosition(task: Task, where: "top" | "bottom") {
  // 正在编辑且该行有未保存输入：整个动作跳过（既不请求后端也不改 store），否则 setPosition 里的
  // replaceTask 会换掉行对象，TaskForm 对 props.task 是 deep watch → loadFrom 用服务端值重建草稿，
  // 用户刚打的字丢失。跟 dragwrap 上「编辑中的行不给拖拽」同一思路，只是这里必须连请求一起跳过：
  // 只跳过本地 patch 的话，随后保存会把旧 sort_order PUT 回去，置顶反而静默失效。
  if (editingId.value === task.id && dirty.value.has(task.id)) {
    ElMessage.warning("该行正在编辑且有未保存的修改，请先保存再调整排序");
    return;
  }
  try {
    await tasks.setPosition(task.id, where);
    ElMessage.success(where === "top" ? "已置顶" : "已置底");
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

// —— 拖拽排序 ——
const dragId = ref<number | null>(null);
function onDragStart(id: number) {
  if (!canDrag.value) return;
  dragId.value = id;
}
function onDragOver(ev: DragEvent, id: number) {
  if (!canDrag.value || dragId.value === null || dragId.value === id) return;
  ev.preventDefault();
  const order = sorted.value.map((t) => t.id);
  const from = order.indexOf(dragId.value);
  const to = order.indexOf(id);
  if (from < 0 || to < 0) return;
  order.splice(to, 0, order.splice(from, 1)[0]);
  tasks.tasks = order.map((tid, i) => {
    const t = tasks.tasks.find((x) => x.id === tid)!;
    return { ...t, sort_order: i };
  });
}
async function onDragEnd() {
  if (dragId.value === null) return;
  dragId.value = null;
  if (!canDrag.value) return;
  try {
    await tasks.reorder(sorted.value.map((t) => t.id));
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

onMounted(() => {
  tasks.fetchTasks();
  accountsStore.fetchAccounts();
});
</script>

<template>
  <div>
    <div class="sticky-bar">
      <span class="sticky-bar__title"
        >任务
        <span v-if="hasUnsaved" class="dot-unsaved" title="有未保存的修改" />
      </span>
      <el-button type="primary" @click="openRun(null)"> ▶ 立即运行 </el-button>
      <el-button @click="startNew()"> ＋ 新建任务 </el-button>
    </div>

    <div class="toolbar">
      <el-input v-model="keyword" placeholder="搜索任务名 / 链接" clearable style="max-width: 220px" />
      <el-select v-model="pathFilter" placeholder="按保存路径筛选" clearable style="max-width: 200px">
        <el-option v-for="p in savePaths" :key="p" :label="p" :value="p" />
      </el-select>
      <span class="count text-muted">共 {{ filtered.length }} 个任务</span>
      <span class="spacer" />
      <el-button @click="importClipboard"> 📋 剪贴板导入 </el-button>
    </div>

    <p v-if="tasks.error" class="state err">
      {{ tasks.error }}
    </p>
    <div v-else-if="!filtered.length && !tasks.loading && editingId !== 'new'" class="empty-state card">
      <span class="emoji">🗂️</span>
      还没有任务，点击右上角「新建任务」开始追更吧
    </div>

    <div v-else v-loading="tasks.loading" class="list">
      <p v-if="!canDrag" class="hint text-muted">搜索/筛选状态下已暂停拖拽排序</p>
      <div
        v-for="(t, i) in filtered"
        :key="t.id"
        class="dragwrap"
        :draggable="canDrag && editingId !== t.id"
        @dragstart="onDragStart(t.id)"
        @dragover="onDragOver($event, t.id)"
        @dragend="onDragEnd"
      >
        <TaskRow
          :task="t"
          :index="i"
          :dirty="dirty.has(t.id)"
          :expanded="editingId === t.id"
          @toggle="toggleExpand(t.id)"
          @run="openRun(t)"
          @position="onPosition(t, $event)"
        />
        <TaskForm
          v-if="editingId === t.id"
          :task="t"
          :accounts="accounts"
          @save="onSave"
          @cancel="editingId = null"
          @remove="onRemove"
          @dirty="onDirty"
        />
      </div>

      <div v-if="editingId === 'new'" class="dragwrap">
        <div class="card new-head">新建任务</div>
        <TaskForm
          :task="null"
          :accounts="accounts"
          :prefill="pendingPrefill"
          @save="onSave"
          @cancel="editingId = null"
          @dirty="onDirty"
        />
      </div>
    </div>

    <RunLogDialog v-model="runDialog" :task-id="runTaskId" :name="runName" />
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.count {
  font-size: 13px;
}
.spacer {
  flex: 1;
}
.list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.dragwrap {
  position: relative;
}
.dragwrap[draggable="true"] {
  cursor: grab;
}
.hint {
  font-size: 12px;
}
.state.err {
  color: var(--danger);
}
.new-head {
  font-weight: 600;
  border-color: var(--primary);
  background: var(--primary-soft);
  color: var(--primary);
}
</style>
