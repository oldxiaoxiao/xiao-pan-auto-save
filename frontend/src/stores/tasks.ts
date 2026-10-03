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
      tasks.value = list.map((t, i) => ({ ...t, sort_order: t.sort_order ?? i }));
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
      const payload: TaskPayload = {
        taskname: t.taskname,
        shareurl: t.shareurl,
        savepath: t.savepath,
        pattern: t.pattern,
        replace: t.replace,
        ignore_extension: t.ignore_extension,
        startfid: t.startfid,
        update_subdir: t.update_subdir,
        update_subdir_resave: t.update_subdir_resave,
        enddate: t.enddate,
        auto_download: t.auto_download,
        download_subdir: t.download_subdir,
        download_savepath: t.download_savepath,
        runweek: t.runweek,
        disabled: t.disabled,
        account_id: t.account_id,
        sort_order: t.sort_order,
      };
      const updated = await api.updateTask(t.id, payload);
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
    reorder,
  };
});
