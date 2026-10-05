import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { api } from "../api/client";
import type { Task, TaskPayload } from "../api/types";

/** 任务列表 store：拉取、CRUD 封装、脏标记、批量排序。 */
export const useTasksStore = defineStore("tasks", () => {
  const tasks = ref<Task[]>([]);
  const loading = ref(false);
  const error = ref("");
  /** 有未保存修改的任务 id 集合（新建草稿用 "new"）。 */
  const dirty = ref<Set<number>>(new Set());

  const sorted = computed(() => [...tasks.value].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id));
  const savePaths = computed(() => {
    const set = new Set<string>();
    for (const t of tasks.value) if (t.savepath) set.add(t.savepath);
    return [...set].sort();
  });

  function markDirty(id: number, value: boolean) {
    if (value) dirty.value.add(id);
    else dirty.value.delete(id);
    dirty.value = new Set(dirty.value);
  }

  async function fetchTasks() {
    loading.value = true;
    error.value = "";
    try {
      const list = await api.listTasks();
      tasks.value = list;
      dirty.value = new Set();
    } catch (e) {
      error.value = (e as Error).message;
    } finally {
      loading.value = false;
    }
  }

  function replaceTask(next: Task) {
    const idx = tasks.value.findIndex((t) => t.id === next.id);
    if (idx >= 0) tasks.value[idx] = next;
    else tasks.value.push(next);
    tasks.value = [...tasks.value];
  }

  async function createTask(payload: TaskPayload): Promise<Task> {
    const created = await api.createTask(payload);
    replaceTask(created);
    return created;
  }

  async function updateTask(id: number, payload: TaskPayload): Promise<Task> {
    const updated = await api.updateTask(id, payload);
    replaceTask(updated);
    markDirty(id, false);
    return updated;
  }

  async function deleteTask(id: number) {
    await api.deleteTask(id);
    tasks.value = tasks.value.filter((t) => t.id !== id);
    markDirty(id, false);
  }

  /** 显式置顶/置底：后端回新 sort_order，就地更新让 sorted 重排。
   *  这里换的是整行对象引用，而 TaskForm 对 props.task 是 deep watch，会拿服务端值重建草稿——
   *  所以「正在编辑且未保存」那一行必须由 TasksView.onPosition 先拦住（连请求一起跳过），别在这儿 patch。 */
  async function setPosition(id: number, where: "top" | "bottom") {
    const current = tasks.value.find((t) => t.id === id);
    if (!current) return;
    const res = await api.setTaskPosition(id, where);
    replaceTask({ ...current, sort_order: res.sort_order });
  }

  /** 按给定 id 顺序批量更新 sort_order（仅提交变化项）。 */
  async function reorder(orderedIds: number[]) {
    const byId = new Map(tasks.value.map((t) => [t.id, t]));
    const changes: Task[] = [];
    orderedIds.forEach((id, i) => {
      const t = byId.get(id);
      if (t && t.sort_order !== i) {
        const next = { ...t, sort_order: i };
        changes.push(next);
      }
    });
    for (const t of changes) {
      // 剥离只读字段，其余即 TaskPayload；新增字段自动随展开带上，避免手写清单漏字段
      const { id, shareurl_ban, last_run_at, ...payload } = t;
      const updated = await api.updateTask(id, payload);
      replaceTask(updated);
    }
  }

  return {
    tasks,
    sorted,
    savePaths,
    loading,
    error,
    dirty,
    markDirty,
    fetchTasks,
    createTask,
    updateTask,
    deleteTask,
    setPosition,
    reorder,
  };
});
