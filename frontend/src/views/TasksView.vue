<script setup lang="ts">
import { ref, computed, onMounted, onErrorCaptured } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { storeToRefs } from "pinia";
import { useTasksStore } from "../stores/tasks";
import { useAccountsStore } from "../stores/accounts";
import { useSettingsStore } from "../stores/settings";
import TaskRow from "../components/TaskRow.vue";
import TaskForm from "../components/TaskForm.vue";
import RunLogDialog from "../components/RunLogDialog.vue";
import type { Task, TaskPayload } from "../api/types";

const tasks = useTasksStore();
const accountsStore = useAccountsStore();
const settingsStore = useSettingsStore();
const { sorted, savePaths, dirty } = storeToRefs(tasks);
const { accounts } = storeToRefs(accountsStore);
// 「那份真默认值到手了没」由 store 自己回答（settings 是 Settings | null，ready 是它的派生状态），
// 视图里不再自己拿 settings.task_defaults 去猜。
const { ready: defaultsReady } = storeToRefs(settingsStore);

// 只有「＋ 新建任务」依赖 task_defaults：新建没有来源数据，初值只能来自后端那一份（前端不写第二份）。
// 拉取失败是另一种"没载入完"，而且它永远载入不完：loading 已经收起、task_defaults 永远缺席。
// 此时再说"正在读取默认值…"就是承诺一件不会发生的事，所以用 store.error 换成可操作的失败态。
const defaultsFailed = computed(() => !defaultsReady.value && !!settingsStore.error);
// 失败态不算"等待"（按钮要能点下去重试），其余未就绪都算：等待中按钮禁用。
const newBlocked = computed(() => !defaultsReady.value && !defaultsFailed.value);
const newLabel = computed(() => {
  if (defaultsFailed.value) return "默认值读取失败，点击重试";
  if (newBlocked.value) return "正在读取默认值…";
  return "＋ 新建任务";
});

const keyword = ref("");
const pathFilter = ref("");
const editingId = ref<number | "new" | null>(null);
const newDirty = ref(false);

const runDialog = ref(false);
const runTaskId = ref<number | null>(null);
const runName = ref("");
const pendingPrefill = ref<Partial<TaskPayload> | undefined>(undefined);

const onlyIssues = ref(false);

const issueCount = computed(() => sorted.value.filter((t) => (t.health?.status || "ok") !== "ok").length);

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  return sorted.value.filter((t) => {
    if (onlyIssues.value && (t.health?.status || "ok") === "ok") return false;
    if (pathFilter.value && t.savepath !== pathFilter.value) return false;
    if (!kw) return true;
    return t.taskname.toLowerCase().includes(kw) || t.shareurl.toLowerCase().includes(kw);
  });
});

const canDrag = computed(() => !keyword.value.trim() && !pathFilter.value && !onlyIssues.value);
const hasUnsaved = computed(() => dirty.value.size > 0 || newDirty.value);

/**
 * 离开某个已展开的编辑行：那一行的 TaskForm 会卸载、草稿随即用服务端值重建，
 * store 里的脏标记若留着就是谎报（顶部「有未保存的修改」小圆点一直亮着，其实什么都没剩）。
 * 只要编辑焦点从该行挪走就显式清掉——这也是下面那道脏守卫"标记可信"的前提。
 */
function leaveRowEditor() {
  if (typeof editingId.value === "number") tasks.markDirty(editingId.value, false);
}

/**
 * 会丢草稿的动作（切到另一行、收起当前行、新建、复制、剪贴板导入）共用这一句点名。
 * 说清是哪一份草稿，别让用户猜"我刚才改的是哪行"。
 */
const draftName = computed(() => {
  if (editingId.value === "new") return "新建任务的表单";
  if (typeof editingId.value === "number") {
    const t = sorted.value.find((x) => x.id === editingId.value);
    return `《${t?.taskname ?? editingId.value}》`;
  }
  return "";
});

/**
 * 丢草稿前的确认门。
 *
 * 这里刻意不再"闷着 return + 一条 3 秒就消失的 toast"：那样用户看到的就是"点了没反应"，
 * 而且一旦进入这个状态，之后每一次点行、点「＋ 新建任务」都会被同一道门挡下又只弹同样的 toast，
 * 整个任务页像是卡死（桌面客户端窗口里尤其明显）。现在改成明明白白的二选一：
 * 确认 = 丢弃并继续（点击必然有结果），取消/ESC/点遮罩 = 原地不动，草稿还在，回去点「保存」即可。
 */
async function confirmDiscard(): Promise<boolean> {
  if (!hasUnsaved.value) return true;
  try {
    await ElMessageBox.confirm(
      `${draftName.value}有未保存的修改，继续将丢弃它们。返回后可先点表单上的「保存」。`,
      "有未保存的修改",
      { type: "warning", confirmButtonText: "丢弃并继续", cancelButtonText: "返回继续编辑" },
    );
    return true;
  } catch {
    // cancel 与关闭（ESC / 点遮罩）同义：都不动草稿，也不改 editingId。
    return false;
  }
}

/** 确认丢弃后执行：行内脏标记与新建草稿标记一起清，否则顶部小圆点会谎报"还有未保存的修改"。 */
function discardDraft() {
  leaveRowEditor();
  newDirty.value = false;
}

async function toggleExpand(id: number) {
  const current = editingId.value;
  // 收起当前行同样是"丢草稿"，与切到另一行走同一道门。
  if (!(await confirmDiscard())) return;
  discardDraft();
  editingId.value = current === id ? null : id;
}

async function startNew() {
  // 新建的门槛：必须有那份真默认值（TaskForm 的 blank(defaults) 要用它当表单初值），
  // 没拿到时（等待中或已读取失败）都不许实例化新建表单。
  // 编辑已有任务不走这道门——那一行自带全部字段，见下面 TaskForm 的注释。
  if (!defaultsReady.value) return;
  if (!(await confirmDiscard())) return;
  discardDraft();
  pendingPrefill.value = undefined;
  editingId.value = "new";
}

// 同一个按钮位：未就绪时是重试，就绪后才是新建。失败态不新增一次启动拉取，只有用户点击才重拉。
function onNewClick() {
  if (defaultsFailed.value) {
    settingsStore.load();
    return;
  }
  void startNew();
}

/** 表单「取消」：用户明说不要这份草稿，不必再问一次，直接卸载并清掉脏标记。 */
function onCancel() {
  leaveRowEditor();
  newDirty.value = false;
  editingId.value = null;
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

/**
 * 从一段文本里挑出第一条夸克分享链接（只认夸克：目前只有它的驱动是 supported，
 * 别的网盘链接带进来也建不出能跑的任务）。
 */
function pickShareUrl(text: string): string | null {
  return text.match(/https?:\/\/pan\.quark\.cn\/s\/\w+[^\s"'《》]*/)?.[0] ?? null;
}

/** 读剪贴板。读不到一律返回空串而不是抛错——抛错只会让调用方多一层分支，这里"没读到"就够了。 */
async function readClipboard(): Promise<string> {
  try {
    // 桌面客户端是 http://127.0.0.1:随机端口，各家 WebView 对剪贴板权限的判定并不一致
    // （WKWebView 尤其常直接拒绝），所以"读不到"是常态，不是异常。
    return await navigator.clipboard.readText();
  } catch {
    return "";
  }
}

/**
 * 手动粘贴兜底：剪贴板读不到时（非 https、未授权、WebView 拒绝）必须还有一条能走通的路，
 * 否则这个入口在客户端里就是个点了报错的死按钮。
 */
async function askManualUrl(): Promise<string | null> {
  try {
    const { value } = await ElMessageBox.prompt(
      "把夸克分享链接粘贴进来（也可以先复制好再回来点这个入口）",
      "手动导入链接",
      {
        confirmButtonText: "导入",
        cancelButtonText: "取消",
        inputPlaceholder: "https://pan.quark.cn/s/...",
        inputType: "textarea",
        inputValidator: (v: string) =>
          pickShareUrl(v ?? "") ? true : "没找到夸克分享链接，形如 https://pan.quark.cn/s/...",
      },
    );
    return pickShareUrl(String(value ?? ""));
  } catch {
    return null; // 取消 / 关闭：什么都不做
  }
}

async function importClipboard() {
  if (!(await confirmDiscard())) return;
  if (!defaultsReady.value) {
    // 与新建按钮同一套口径：读取失败别再谎称"正在读取"。
    ElMessage.warning(defaultsFailed.value ? "默认值读取失败，请先点「＋ 新建任务」重试" : "正在读取默认值…");
    return;
  }
  // 先试剪贴板，读不到（或读到的不是夸克链接）就原地弹框让人粘贴——一条路走到底，不留死按钮。
  const url = pickShareUrl(await readClipboard()) ?? (await askManualUrl());
  if (!url) return;
  discardDraft();
  editingId.value = "new";
  pendingPrefill.value = { shareurl: url };
  ElMessage.success("已读取分享链接，请补全信息");
}

// —— 复制为新任务：把来源行的业务字段灌进现成的 pendingPrefill 通道，走新建表单 ——
async function startCopy(task: Task) {
  // 与「＋ 新建任务」同一道门（同一个 defaultsReady，不写第二份判空）：那份真默认值没到手时，
  // 新建表单连初值都没有，复制入口同样要挡住。这一条不能像原来那样闷着返回——
  // ⋮ 菜单项此时仍可点，点了没反应会被当成"复制坏了"，所以照 importClipboard 同一口径说清楚。
  if (!defaultsReady.value) {
    ElMessage.warning(defaultsFailed.value ? "默认值读取失败，请先点「＋ 新建任务」重试" : "正在读取默认值…");
    return;
  }
  // 换成 prefill 会整份重建草稿：正在编辑**这一行**、正在编辑别的行、还是新建表单打到一半，
  // 三种都走同一道确认门（口径见 confirmDiscard），不再闷着 return。
  if (!(await confirmDiscard())) return;
  // 剥掉的两类字段：
  // - 服务端进程态（id / shareurl_ban / last_run_at / retry_attempts / next_retry_at / disabled）不属于新任务；
  // - startfid 必剥：它是**来源分享内**的 fid，新任务面对另一条分享时留着会静默截断转存范围（spec 4.3）。
  //   显式给 ""（而不是留 undefined）：prefill 是 Object.assign 覆盖 blank() 的，空串让"起点为空"这条
  //   在数据里说得明明白白，不依赖"键缺席"这种隐式行为。
  // - sort_order 不带 → 新建走后端现成的「新建置顶」（POST /api/tasks 一律 above_sort_order）。
  // 剧名原样带出，不加「副本」后缀——叫什么由用户自己决定。
  const { id, shareurl_ban, last_run_at, retry_attempts, next_retry_at, disabled, sort_order, startfid, ...rest } =
    task;
  // 这八个变量只被解构出来用于剥离，一个都不进 prefill；void 是向 lint 如实声明"故意不用"，不是遗漏。
  void [id, shareurl_ban, last_run_at, retry_attempts, next_retry_at, disabled, sort_order, startfid];
  pendingPrefill.value = { ...rest, startfid: "" };
  discardDraft();
  editingId.value = "new";
}

// —— 显式置顶 / 置底（不受搜索、筛选禁用拖拽的影响）——
async function onPosition(task: Task, where: "top" | "bottom") {
  // 正在编辑且该行有未保存输入：整个动作跳过（既不请求后端也不改 store），否则 setPosition 里的
  // replaceTask 会换掉行对象，TaskForm 对 props.task 是 deep watch → loadFrom 用服务端值重建草稿，
  // 用户刚打的字丢失。跟 dragwrap 上「编辑中的行不给拖拽」同一思路，只是这里必须连请求一起跳过：
  // 只跳过本地 patch 的话，随后保存会把旧 sort_order PUT 回去，置顶反而静默失效。
  // 这条问的是**这一行**的草稿（别的行/新建表单不影响本次排序），所以不走 confirmDiscard 的全局口径。
  if (editingId.value === task.id && dirty.value.has(task.id)) {
    try {
      await ElMessageBox.confirm(
        `《${task.taskname}》有未保存的修改，调整排序会用服务端的值重建这一行、丢弃这些修改。`,
        "有未保存的修改",
        { type: "warning", confirmButtonText: "丢弃并置顶/置底", cancelButtonText: "返回继续编辑" },
      );
    } catch {
      return;
    }
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

/**
 * 表单渲染出错时**不要把整个页面带走**。
 *
 * 展开任务详情就是挂载 TaskForm；它一旦在渲染里抛错，默认会让 TasksView 乃至整个应用
 * 渲染失败——用户看到的就是"从这里开始，点什么都没反应"。这里就地截住：说清错误、
 * 收起编辑态回到能用的列表，页面其余部分照常工作。
 */
onErrorCaptured((err) => {
  ElMessage.error(`任务表单加载失败：${err instanceof Error ? err.message : String(err)}`);
  editingId.value = null;
  newDirty.value = false;
  return false; // 不再向上冒泡，避免整个应用被这一处错误拖垮
});

onMounted(() => {
  tasks.fetchTasks();
  accountsStore.fetchAccounts();
  // 本视图自己拉一次设置：只有「＋ 新建任务」的初值靠 task_defaults，不拉就一直停在"正在读取默认值…"。
  // 编辑已有任务不等它（那一行的值就是初值），所以这里拉取失败也不会把列表里的行锁死。
  settingsStore.load();
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
      <!-- 三种状态各有各的说法：等待中才是"正在读取"，读取失败改成可操作的重试提示。 -->
      <el-button :disabled="newBlocked" @click="onNewClick()">
        {{ newLabel }}
      </el-button>
    </div>

    <div class="toolbar">
      <el-input v-model="keyword" placeholder="搜索任务名 / 链接" clearable style="max-width: 220px" />
      <el-select v-model="pathFilter" placeholder="按保存路径筛选" clearable style="max-width: 200px">
        <el-option v-for="p in savePaths" :key="p" :label="p" :value="p" />
      </el-select>
      <el-checkbox v-model="onlyIssues" :disabled="!issueCount"> 只看待处理（{{ issueCount }}） </el-checkbox>
      <span class="count text-muted">共 {{ filtered.length }} 个任务</span>
      <span class="spacer" />
      <el-button @click="importClipboard"> 📋 导入链接（剪贴板 / 手动粘贴） </el-button>
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
      <!-- 拖拽起点是 TaskRow 里那枚手柄（见那里的注释），行本身不 draggable：
           整行可拖会让带一点位移的普通点击被内核判成拖拽，click 直接不来。 -->
      <div v-for="(t, i) in filtered" :key="t.id" class="dragwrap" @dragover="onDragOver($event, t.id)">
        <TaskRow
          :task="t"
          :index="i"
          :dirty="dirty.has(t.id)"
          :expanded="editingId === t.id"
          :can-drag="canDrag && editingId !== t.id"
          @toggle="toggleExpand(t.id)"
          @run="openRun(t)"
          @position="onPosition(t, $event)"
          @copy="startCopy(t)"
          @dragstart="onDragStart(t.id)"
          @dragend="onDragEnd"
        />
        <!-- 编辑已有任务不依赖 task_defaults：表单每个字段都由那一行自己提供，设置没读到也照样要能编辑，
             否则点行只是"箭头动了一下"的死点击。新建（下面 editingId === 'new'）才需要那份真默认值。 -->
        <TaskForm
          v-if="editingId === t.id"
          :task="t"
          :accounts="accounts"
          @save="onSave"
          @cancel="onCancel"
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
          @cancel="onCancel"
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
