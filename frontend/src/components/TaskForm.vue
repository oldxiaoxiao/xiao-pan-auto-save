<script setup lang="ts">
import { reactive, ref, watch, computed, toRaw } from "vue";
import { ElMessage } from "element-plus";
import FileSelector from "./FileSelector.vue";
import NameTemplatePicker from "./NameTemplatePicker.vue";
import SearchSuggest from "./SearchSuggest.vue";
import { api } from "../api/client";
import { useSettingsStore } from "../stores/settings";
import { useTasksStore } from "../stores/tasks";
import type { Account, DryRunResult, MagicExpand, Task, TaskDefaults, TaskPayload } from "../api/types";
import { MAGIC_VARIABLES, QUALITY_OPTIONS, RUN_MODE_OPTIONS } from "../constants";
import { WEEK_LABELS, formatSize } from "../utils";

/** 首屏那个「子目录也按集数/画质过滤」开关所代表的那一档递归正则。 */
const DEFAULT_SUBDIR_REGEX = ".*"; // 与后端 spec 约定同值：首屏开关"开"就是它，不是别的递归正则

/** 目录内过滤开关的副文案（spec 4.5 逐字）。 */
const SUBDIR_FILTER_HINT = "填了起始集、结束集或画质时，分享里的文件夹会进去逐个文件比对；不填则整个文件夹原样搬走";

const settingsStore = useSettingsStore();
// 同路径判重只用现成的任务列表（store 已加载），不新增任何网络请求。
const tasksStore = useTasksStore();
// settings 是 Settings | null（GET /api/settings 没成功就是没数据），所以这里读出来可能是 undefined：
// 只有「新建」需要它，编辑态的初值来自那一行自己（见 loadFrom）。
const defaults = computed<TaskDefaults | undefined>(() => settingsStore.settings?.task_defaults);

// 新建时的初值**全部来自后端的 task_defaults**，前端不写第二份默认值。
// 为此：settings 没到位时不允许打开新建表单（门槛在 TasksView 的「＋ 新建任务」按钮上，
// 编辑已有任务不受它影响），于是 `blank()` 只会在拿到一份真默认值时被调用，
// 不需要 DEFAULTS_FALLBACK 这种手抄兜底。
function blank(d: TaskDefaults): TaskPayload & { startfid_name: string } {
  return {
    taskname: "",
    shareurl: "",
    savepath: d.savepath_root, // 剧名填上后由下面的 watch 拼成 {root}/{剧名}
    pattern: d.pattern,
    replace: "",
    ignore_extension: false,
    startfid: "",
    startfid_name: "",
    update_subdir: d.subdir_filter ? DEFAULT_SUBDIR_REGEX : "",
    update_subdir_resave: false,
    enddate: "",
    runweek: [],
    auto_download: d.auto_download,
    run_mode: d.run_mode,
    download_subdir: false,
    download_savepath: "",
    disabled: false,
    account_id: null,
    account_failover: true, // FR-06：指定账号失效时默认切到同网盘的其它可用账号
    sort_order: 0,
    episode_start: 0,
    episode_end: 0,
    quality: d.quality,
    schedule: "",
    notify_info: true, // FR-04：默认收转存成功摘要，用户可在本表单按任务关掉
  };
}

const props = defineProps<{
  task: Task | null;
  accounts: Account[];
  prefill?: Partial<TaskPayload>;
}>();
const emit = defineEmits<{
  (e: "save", payload: TaskPayload): void;
  (e: "cancel"): void;
  (e: "remove", id: number): void;
  (e: "dirty", v: boolean): void;
}>();

// draft 的初值全部由下面的 loadFrom 填（那条 watch 带 immediate，setup 阶段就会同步跑一次，渲染前必然是满的）。
// 刻意不在 setup 里 `blank(defaults.value)`：那会让"编辑已有任务"也依赖 settings 到位，
// 设置没读到时点一行只会看到箭头动了一下、表单什么都不渲染的死点击。
const draft = reactive({} as TaskPayload & { startfid_name: string });
let original = "";

// draft.quality 以逗号分隔存储；UI 用数组双向映射
const qualityList = computed({
  get: () => (draft.quality ? draft.quality.split(",").filter(Boolean) : []),
  set: (v: string[]) => (draft.quality = v.join(",")),
});
const advancedOpen = ref<string[]>([]); // 高级区默认收起

// 只有「定时追更」才需要配置频率/星期/截止日期；其余形态收起来，但 draft.schedule 等值按设计保留不清空。
const isFollow = computed(() => draft.run_mode === "follow");

// 2) 路径跟随：只有"新建 + 未手改过路径"才跟随剧名
const pathTouched = ref(false);
/**
 * prefill 显式带了 savepath（Task 4 的「复制为新任务」带原路径）时按 prefill 为准，剧名不把它盖掉。
 *
 * 这是**刻意**的行为，看着反直觉，别当 bug 抹掉：
 * - 它不是"用户手改过一次"。`pathTouched` 的置真点严格只有两处（保存路径输入框的 @input、
 *   文件选择器回传 payload.path），程序代填一律不置真——这条规矩没有因为下面这行而变成三处。
 * - 它代表的是**另一条任务已经定制好的路径**，属于要原样带出来的来源数据，不是本轮输入。
 *   复制出来的任务本该落在同一个目录里；若让剧名跟随把它盖成 {root}/{剧名}，
 *   「复制为新任务」就变成"复制并搬家"，用户会在完全不同的目录里发现一份重复任务。
 * - 所以新建表单的跟随只让位于两种情况：用户改过（pathTouched）、复制带来的原路径（这个标志）。
 *   今天唯一的 prefill 来源是剪贴板导入的 `{ shareurl }`（不带 savepath），该标志不会被触发；
 *   它是为 Task 4 的复制入口预留的，不是死代码。
 */
const prefillPinnedPath = ref(false);
watch(
  () => draft.taskname,
  (name) => {
    // 四条前置 return：编辑态永不改 / 用户手改过不抢 / prefill 带来的原路径不盖（第三条是刻意例外，见上）
    // / 那份真默认值没到手就不猜保存根目录（新建的门槛在 TasksView，正常情况下不会走到最后一条）。
    if (props.task || pathTouched.value || prefillPinnedPath.value) return;
    const root = defaults.value?.savepath_root;
    if (root === undefined) return;
    draft.savepath = name.trim() ? `${root}/${name.trim()}` : root;
  },
);

// 3) 抓取范围是 draft.pattern 的视图，不是第二个状态源。互斥的单选钮只承载两档**真状态**
//    （"" = 全部文件、"$TV" = 只抓剧集）；「自己写正则」是紧挨着它们的**动作项**而不是第三档，
//    因为它不写值：把它放进 radio 组时，点下去 pattern 不变、派生视图立刻回弹到原档位，
//    看起来就像控件坏了（上一轮交代的正是这条缺陷）。所以它从选择控件里挪出来，只做展开高级区 + 聚焦。
const CAPTURE_CUSTOM = "custom"; // 哨兵：pattern 是别的自定义值时不匹配任何一档，radio 组如实显示为无选中
const captureMode = computed<"all" | "tv" | typeof CAPTURE_CUSTOM>({
  get: () => (draft.pattern === "" ? "all" : draft.pattern === "$TV" ? "tv" : CAPTURE_CUSTOM),
  set: (mode) => {
    if (mode === "all") draft.pattern = "";
    else if (mode === "tv") draft.pattern = "$TV";
    // 两档之外的值一律不改 pattern：自定义正则只能由高级区那条输入框改动，这里不为此再造状态位。
  },
});
/** 当前是既非 "" 也非 "$TV" 的自定义正则：两档都不该被"假装选中"，如实点名当前值来自高级设置那一条。 */
const isCustomPattern = computed(() => captureMode.value === CAPTURE_CUSTOM);

/** 高级区那条 pattern 输入框的句柄：「自己写正则」这个动作只负责展开高级区并把光标落上去。 */
const patternInput = ref<{ focus?: () => void } | null>(null);
function openAdvancedToPattern() {
  advancedOpen.value = ["adv"];
  requestAnimationFrame(() => patternInput.value?.focus?.());
}

// $TV 的真实正则只从 /api/settings/magic/expand 取，前端不抄第二份（展开逻辑只住在后端一处）。
const tvExpand = ref<MagicExpand | null>(null);
const tvExpandError = ref("");
async function loadTvExpand() {
  tvExpandError.value = "";
  try {
    tvExpand.value = await api.magicExpand("$TV");
  } catch (e) {
    tvExpand.value = null;
    tvExpandError.value = (e as Error).message;
  }
}
watch(captureMode, (m) => (m === "tv" ? void loadTvExpand() : undefined), { immediate: true });

// 4) 目录内过滤开关：只认 "" 和 ".*" 两个值，自定义递归正则不许被开关抹掉。
const subdirFilterOn = computed({
  get: () => draft.update_subdir !== "",
  set: (on) => {
    if (on) draft.update_subdir = draft.update_subdir === "" ? DEFAULT_SUBDIR_REGEX : draft.update_subdir;
    else if (draft.update_subdir === DEFAULT_SUBDIR_REGEX) draft.update_subdir = "";
    // 关但值是别的递归正则：保持不动，让高级区那一条继续管，别静默改写用户手写的正则
  },
});
/** 递归正则是用户手写的自定义值：开关显示为开，但既不覆盖也不清空，改由高级区那一条管。 */
const subdirCustom = computed(() => draft.update_subdir !== "" && draft.update_subdir !== DEFAULT_SUBDIR_REGEX);

/** 起点合并：startfid 优先于集数（engine 遍历到该 fid 即 break，集数过滤在其之后仍生效）。 */
const startFidLabel = computed(() => draft.startfid_name || draft.startfid);

/** 抓取范围的两档真状态；「自己写正则」不在这份列表里——它不写值，只是进高级区的动作。 */
const CAPTURE_OPTIONS: { value: "all" | "tv"; label: string }[] = [
  { value: "all", label: "全部文件" },
  { value: "tv", label: "只抓剧集" },
];

/** 两档各自的说明（正则原文只从 /api/settings/magic/expand 取，前端一份都不抄）。文案一律用户视角：只说这一档会转什么、拿没拿到。 */
const captureHead = computed(() => {
  if (captureMode.value === "tv") return "只挑剧集文件，跳过特典、字幕、说明之类别的内容";
  if (isCustomPattern.value) return `按你自己写的正则挑选文件：${draft.pattern}`;
  return "全部文件：分享目录里有什么就转什么，只受下面的集数与画质过滤约束";
});

/** 上面那句话的补充：自定义值态说明去哪儿改；只抓剧集那档给展开式的读取进度与失败原因原文。正则本身另用等宽 code 块显示，不混进句子。 */
const captureNote = computed(() => {
  if (isCustomPattern.value)
    return "这个值不属于上面任何一档，所以两档都没有选中；要改动请点「自己写正则」，在高级设置里那条匹配正则中修改。";
  if (captureMode.value !== "tv") return "";
  // 拿不到正则时不许显示成空白：读取失败就把端点回传的原因原样贴出来。
  if (tvExpandError.value) return `展开后的正则读取失败：${tvExpandError.value}`;
  if (tvExpand.value === null) return "正在读取展开后的正则…";
  if (!tvExpand.value.ok) return "当前「魔法匹配」里还没有 $TV 这一项，没有可展开的正则。";
  return "命中下面这条正则的文件才会被转存：";
});

/** $TV 展开后的真实正则原文（等宽显示用）：只在「只抓剧集」这一档出现，切走就不念旧值。 */
const expandedPattern = computed(() =>
  captureMode.value === "tv" && tvExpand.value?.ok ? tvExpand.value.pattern : "",
);

const pathHint = computed(() => {
  if (props.task) return "编辑已有任务时，路径不会跟着剧名自动改动。";
  const root = defaults.value?.savepath_root;
  const tail = "手动改过一次、或用「选择」定过一次之后就不再跟。";
  // 新建表单的门槛在 TasksView，正常情况下这里一定拿得到保存根目录；真没到手就说人话，不渲染出 undefined。
  return root === undefined ? `新建时路径跟着剧名走；${tail}` : `新建时路径跟着剧名走（${root}/{剧名}）；${tail}`;
});

/** 同路径提示（spec 4.3 逐字文案的触发条件）：只有 prefill 带来过 savepath（「复制为新任务」）才判重——
 *  普通新建的路径由剧名现拼，撞不撞由用户自己定；复制则是明确把"另一条任务的路径"带了进来，
 *  同名文件会被引擎认成"已存在"而跳过，必须点名。比对对象是当前 draft.savepath（用户手改后跟着改），
 *  数据源是内存里的 tasks store，不发请求。 */
const duplicatePath = computed(
  () =>
    !!props.prefill?.savepath && tasksStore.tasks.some((t) => t.savepath === draft.savepath && t.id !== props.task?.id),
);

const startHint = computed(() =>
  startFidLabel.value
    ? `起点是《${startFidLabel.value}》：这一条和比它更新的都会转，更早的跳过（集数、画质过滤照旧生效）`
    : "不选文件就从最开头开始；集数填 0 表示不限。",
);

/** 「停用」在编辑态才有意义；这句是既有改形态提示，只在停用 + 非定时追更时说得出。 */
const disabledNote = computed(() =>
  !isFollow.value && draft.disabled ? "该行当前为停用状态；改回「定时追更」后需手动取消停用才会恢复自动运行。" : "",
);

/** 目录内过滤开关的副文案；自定义递归正则时追加点名高级区的说明，但诚实提示永远在。 */
const subdirHint = computed(() => {
  // engine.py 的既有行为：子目录里的文件不参与魔法重命名。本轮没改引擎，这句不许被写成"已修好"，也不许被藏掉。
  const honest = "子目录内的文件不参与魔法重命名";
  if (!subdirCustom.value) return honest;
  return `当前子目录递归正则是自定义值 ${draft.update_subdir}，开关不会覆盖它，要改请回高级设置那条 update_subdir。${honest}`;
});

// —— 更新频率 ——
const SCHEDULE_PRESETS = [
  { label: "继承全局", value: "" },
  { label: "每 5 分钟", value: "interval:5" },
  { label: "每 30 分钟", value: "interval:30" },
  { label: "每小时", value: "interval:60" },
  { label: "每天 9:00", value: "cron:0 9 * * *" },
  { label: "每周一 9:00", value: "cron:0 9 * * 1" },
  { label: "每周二 9:00", value: "cron:0 9 * * 2" },
  { label: "每周三 9:00", value: "cron:0 9 * * 3" },
  { label: "每周四 9:00", value: "cron:0 9 * * 4" },
  { label: "每周五 9:00", value: "cron:0 9 * * 5" },
  { label: "每周六 9:00", value: "cron:0 9 * * 6" },
  { label: "每周日 9:00", value: "cron:0 9 * * 0" },
  { label: "自定义 cron", value: "__custom" },
];
const customCron = ref("");
const scheduleMode = computed({
  get: () => {
    const s = draft.schedule;
    if (s.startsWith("cron:") && !SCHEDULE_PRESETS.some((p) => p.value === s)) return "__custom";
    return s;
  },
  set: (v: string) => {
    if (v === "__custom") draft.schedule = `cron:${customCron.value}`;
    else draft.schedule = v;
  },
});
watch(customCron, (c) => {
  if (scheduleMode.value === "__custom") draft.schedule = `cron:${c}`;
});

function loadFrom(task: Task | null) {
  pathTouched.value = false; // 每次载入重来：新建表单要能跟随剧名
  prefillPinnedPath.value = false;
  if (task) {
    // 编辑态以服务端那一行为基底：Task 带齐 TaskPayload 的每个键，唯一要补的是 UI 专用的 startfid_name。
    // 安全的原因：pattern / quality / run_mode / auto_download / update_subdir / savepath 这些恰好是
    // task_defaults 会盖住的键，编辑本来就该以那一行自己的值为准（服务端权威），前端没有任何可猜的空间；
    // 所以 settings 没读到（task_defaults 缺席）也照样能渲染编辑表单，不再是个死点击。
    Object.assign(draft, toRaw(task), { startfid_name: "" });
    // 老服务端/旁路写入的行可能还没有这个字段：缺了按"开"处理，
    // 不能让 undefined 在开关上显示成关闭、保存时静默把通知关掉。
    draft.notify_info = task.notify_info !== false;
    // 同上：补列前的老行没有这个字段，缺了按"开"处理
    draft.account_failover = task.account_failover !== false;
  } else if (defaults.value) {
    // 新建态的初值只能来自那份真默认值；门槛在 TasksView 的「＋ 新建任务」上，这里只做兜底不实例化。
    Object.assign(draft, blank(defaults.value));
  }
  // prefill 在 blank() 之后覆盖；带了 update_subdir 键时以 prefill 为准，不再按 subdir_filter 默认改写
  // （Task 4 的「复制为新任务」靠这条把原任务的递归正则原样带出来）。
  if (!task && props.prefill) {
    Object.assign(draft, props.prefill);
    // 同上口径扩到 savepath：prefill 带了路径就**故意**不再跟随剧名（见上面 prefillPinnedPath 的说明）。
    // 这里用独立标志而不是去置真 pathTouched，正是为了让 pathTouched 的置真点仍然只有"用户亲手改过"两处；
    // 抹掉这一行，复制来的定制路径会被剧名 watch 盖成 {root}/{剧名}，等于复制时静默搬了家。
    if (props.prefill.savepath !== undefined) prefillPinnedPath.value = true;
  }
  // 这两条回填的"来源"口径统一走 source：**编辑态是那一行本身，复制态是 prefill**。
  // startCopy 把来源行的业务字段整份灌进 pendingPrefill，而 runweek 不在 spec 4.3 的剥除清单里（必须带出），
  // 自定义 cron 表达式同理属于那条任务自己的频率。只认 props.task 的话，复制一条设过周几/非预设 cron 的任务
  // 会得到"星期胶囊全空 + 更新频率显示自定义 cron 但输入框为空"，两处同一个根因。
  // 数组照旧拷一份：直接把 store 里那条任务的数组挂进 draft，toggleWeek 一点就改到列表行（编辑态本来就有这层保护）。
  const source = task ?? props.prefill;
  draft.runweek = [...(source?.runweek ?? [])];
  // 若来源的 schedule 是不在预设里的 cron,回填自定义输入框
  const s = source?.schedule ?? "";
  if (s.startsWith("cron:") && !SCHEDULE_PRESETS.some((p) => p.value === s)) customCron.value = s.slice(5);
  else customCron.value = "";
  original = snapshot();
}

function snapshot(): string {
  const { startfid_name, ...rest } = draft;
  void startfid_name;
  // 排序是为了让键序变化不影响脏判定（不顺手剥服务端字段：那是另一件事）。
  const sorted = Object.keys(rest)
    .sort()
    .map((k) => [k, (rest as Record<string, unknown>)[k]]);
  return JSON.stringify(Object.fromEntries(sorted));
}

watch(
  () => [props.task, props.prefill],
  () => loadFrom(props.task),
  { immediate: true, deep: true },
);
watch(snapshot, (s) => emit("dirty", s !== original));

const isDirty = computed(() => snapshot() !== original);

// —— 星期胶囊 ——
function toggleWeek(d: number) {
  const i = draft.runweek.indexOf(d);
  if (i >= 0) draft.runweek.splice(i, 1);
  else draft.runweek.push(d);
}

// —— 魔法变量插入到聚焦输入框光标处 ——
const activeRef = ref<HTMLElement | null>(null);
function onFieldFocus(ev: FocusEvent) {
  activeRef.value = ev.target as HTMLElement;
}
function insertVar(token: string) {
  const el = activeRef.value as HTMLInputElement | null;
  if (!el || typeof el.selectionStart !== "number") {
    draft.replace += token;
    return;
  }
  const key = el.getAttribute("data-field") === "pattern" ? "pattern" : "replace";
  const cur = draft[key];
  const pos = el.selectionStart ?? cur.length;
  draft[key] = cur.slice(0, pos) + token + cur.slice(el.selectionEnd ?? pos);
  requestAnimationFrame(() => {
    el.focus();
    el.setSelectionRange(pos + token.length, pos + token.length);
  });
}

// —— 文件选择器 ——
const selector = ref(false);
const selectorMode = ref<"savepath" | "startfid" | "preview">("savepath");
function openSelector(mode: "savepath" | "startfid" | "preview") {
  selectorMode.value = mode;
  selector.value = true;
}
function onSelectorConfirm(payload: Record<string, string>) {
  if (payload.path) {
    draft.savepath = payload.path;
    pathTouched.value = true; // 用选择器选定路径算"用户手改"
  }
  if (payload.fid) {
    draft.startfid = payload.fid;
    draft.startfid_name = payload.name || payload.fid;
  }
  selector.value = false;
}

function clearStart() {
  draft.startfid = "";
  draft.startfid_name = "";
}

// —— 试跑（只读）——
const dryRunning = ref(false);
const dryResult = ref<DryRunResult | null>(null);

/** FR-10：命名模板库弹窗。 */
const tplOpen = ref(false);
function applyNameTemplate(pattern: string, replace: string) {
  draft.pattern = pattern;
  draft.replace = replace;
}
const dryError = ref("");
/** 顶部那一行：忙态 > 请求异常 > 后端 message 原文（banned 就说 banned，不美化成「没有新文件」）。 */
const dryMessage = computed(() => {
  if (dryRunning.value) return "正在只读访问网盘…（递归目录可能要十几秒）";
  if (dryError.value) return dryError.value;
  return dryResult.value?.message ?? "";
});
const dryMsgClass = computed(() => {
  const bad = !!dryError.value || (dryResult.value !== null && dryResult.value?.ok === false);
  return bad ? "dryrun__msg err" : "dryrun__msg";
});
/**
 * 汇总 + 前 20 条，拼成一段多行文本（pre-line 渲染）。
 * 守卫分支（无支持的驱动 / 无可用账号）只回 ok/status/message/items：计数键缺席时那几行整行不出现，
 * 绝不显示成 0。
 */
const dryBody = computed(() => {
  const r = dryResult.value;
  if (!r) return "";
  const lines: string[] = [];
  if (r.new_count !== undefined) {
    const size = r.total_size ? ` · 约 ${formatSize(r.total_size)}` : "";
    lines.push(`会新增 ${r.new_count} 项${size} · 落到 ${draft.savepath}`);
  }
  const skips: string[] = [];
  if (r.skipped_existing !== undefined) skips.push(`已存在跳过 ${r.skipped_existing} 项`);
  if (r.filtered_out !== undefined) skips.push(`被集数/画质过滤 ${r.filtered_out} 项`);
  if (skips.length) lines.push(skips.join(" · "));
  // 口径：skipped_existing 只统计"非目录条目因为目标目录已有同名文件被跳过"；目录不在这条计数里。
  if (r.skipped_existing !== undefined)
    lines.push("「已存在跳过」只统计文件；目录要么整目录搬走，要么已在目标里本轮不搬，都不在这条计数里");
  for (const it of r.items ?? []) {
    const renamed = it.share_name !== it.final_name ? ` ← ${it.share_name}` : "";
    lines.push(`${it.final_name}${renamed}${it.is_dir ? "（目录）" : ""} · ${it.dest_path}`);
  }
  if ((r.items?.length ?? 0) >= 20) lines.push("只列前 20 条。");
  // FR-10：正则没命中的样本。没有它，"0 个新增"分不清是正则写错还是真的没有更新。
  const unmatched = r.unmatched_samples ?? [];
  if (unmatched.length) {
    lines.push(`⚠️ 有 ${unmatched.length} 个文件没被匹配正则命中，它们不会被转存：` + unmatched.slice(0, 8).join("、"));
    lines.push("如果这不是你想要的，去「命名模板库」换个模板，或把匹配正则留空（留空 = 全部命中）。");
  } else if (r.new_count === 0 && (r.skipped_existing ?? 0) === 0 && (r.filtered_out ?? 0) === 0) {
    lines.push("分享里没有任何待转存条目（不是被过滤掉的），说明目标目录已经是最新的。");
  }
  return lines.join("\n");
});

/**
 * 试跑前的必填校验与 submit 同口径、逐字段点名（spec 4.5）：说清到底是哪一项还没填，
 * 不合并成"请先填写分享链接与保存路径"这种要用户自己对照的提示。
 */
async function tryRun() {
  if (!draft.taskname.trim()) {
    ElMessage.warning("请填写任务名称");
    return;
  }
  if (!draft.shareurl.trim()) {
    ElMessage.warning("请填写分享链接");
    return;
  }
  if (!draft.savepath.trim()) {
    ElMessage.warning("请选择保存路径");
    return;
  }
  dryRunning.value = true;
  dryError.value = "";
  dryResult.value = null; // 清掉上一轮结果，免得忙态里旧的计数被读成这一轮的
  try {
    const { startfid_name, ...payload } = draft;
    void startfid_name;
    dryResult.value = await api.dryRun({ ...payload });
  } catch (e) {
    dryResult.value = null;
    dryError.value = (e as Error).message;
  } finally {
    dryRunning.value = false;
  }
}

function submit() {
  if (!draft.taskname.trim()) return ElMessage.warning("请填写任务名称");
  if (!draft.shareurl.trim()) return ElMessage.warning("请填写分享链接");
  if (!draft.savepath.trim()) return ElMessage.warning("请选择保存路径");
  const { startfid_name, ...payload } = draft;
  void startfid_name;
  // 选中「自定义 cron」却没写表达式时，draft.schedule 是一条半成品（`cron:` 或 `cron:` 加空格），发出去等于
  // 把空表达式当成频率交给后端。这里如实兑现输入框下面那句「否则将回退为继承全局」：空的一律回落成 ""（继承全局），
  // 于是复制、编辑、手选自定义三条路径都不会发出 "cron:"。
  const schedule = /^cron:\s*$/.test(payload.schedule) ? "" : payload.schedule;
  emit("save", { ...payload, schedule });
}

const hasId = computed(() => props.task?.id ?? null);
</script>

<template>
  <div class="form">
    <!-- 复制来的路径与某条已有任务重合时的如实提醒（spec 4.3 逐字）。文案包在无属性 span 里、外层 p 只留 v-if
         一个属性：这是 prettier 与 vue 规则都稳定的最小排版，直排成一行会被记两条排版 warning。 -->
    <p v-if="duplicatePath">
      <span class="dup-path">这条任务和已有任务用了同一个保存路径，同名文件会被认成"已存在"而跳过</span>
    </p>
    <div class="grid">
      <div class="f f--wide">
        <label class="field-label">任务名称 / 智能搜索</label>
        <SearchSuggest v-model:taskname="draft.taskname" v-model:shareurl="draft.shareurl" />
      </div>

      <div class="f f--wide">
        <label class="field-label">分享链接</label>
        <div class="row">
          <el-input v-model="draft.shareurl" placeholder="https://pan.quark.cn/s/..." />
          <el-button :icon="'🔍'" :disabled="!draft.shareurl.trim()" @click="openSelector('preview')"> 浏览 </el-button>
        </div>
      </div>

      <div class="f f--wide">
        <label class="field-label">保存路径</label>
        <div class="row">
          <!-- 占位符只是提示文字：task_defaults 没到手时留空，不前端猜一个保存根目录给用户看。 -->
          <el-input v-model="draft.savepath" :placeholder="defaults?.savepath_root" @input="pathTouched = true" />
          <el-button :icon="'📁'" @click="openSelector('savepath')"> 选择 </el-button>
        </div>
        <!-- pathTouched 只在这两处置真：这条输入框的 @input、以及选择器回传 payload.path；程序代填不置真。 -->
        <small class="hint">{{ pathHint }}</small>
      </div>

      <div class="f f--wide">
        <label class="field-label">抓取范围</label>
        <div class="capture">
          <el-radio-group v-model="captureMode">
            <el-radio-button v-for="o in CAPTURE_OPTIONS" :key="o.value" :value="o.value">{{
              o.label
            }}</el-radio-button>
          </el-radio-group>
          <!-- 动作项而不是第三档：它不写 pattern，只把人带到能改它的地方，于是没有"点下去又弹回来"这回事。
               与本页其它文字链接（如「或从某个文件开始」）同款；键盘用户走下面那条高级设置标题，路径本来就在。 -->
          <span @click="openAdvancedToPattern">自己写正则（展开高级设置）</span>
          <small v-if="isCustomPattern">当前是自定义正则，见高级设置</small>
        </div>
        <!-- 首屏与高级区永远共用 draft.pattern 这一个值，这里也永不出现第二个正则输入框。 -->
        <small class="hint">
          {{ captureHead }}<template v-if="captureNote"> —— {{ captureNote }}</template>
          <code v-if="expandedPattern">{{ expandedPattern }}</code>
        </small>
      </div>

      <div class="f">
        <label class="field-label">从第几集开始（含）</label>
        <el-input-number v-model="draft.episode_start" :min="0" :max="9999" controls-position="right" />
      </div>
      <div class="f">
        <label class="field-label">到第几集结束（含）</label>
        <el-input-number v-model="draft.episode_end" :min="0" :max="9999" controls-position="right" />
      </div>
      <div class="f f--wide">
        <div class="row">
          <span class="link" @click="openSelector('startfid')">或从某个文件开始</span>
          <span v-if="draft.startfid" class="link link--danger" @click="clearStart">清除起点</span>
        </div>
        <!-- 起点合并：startfid 与集数不再并列两个概念，选过文件就如实显示当前生效的起点。 -->
        <small class="hint">{{ startHint }}</small>
      </div>

      <div class="f f--wide">
        <label class="field-label">画质（多选，留空=不限；4K 与 2160P 互为别名）</label>
        <el-select v-model="qualityList" multiple clearable placeholder="不限画质">
          <el-option v-for="q in QUALITY_OPTIONS" :key="q" :label="q" :value="q" />
        </el-select>
      </div>

      <div class="f">
        <label class="field-label">下载到本地</label>
        <el-switch v-model="draft.auto_download" />
      </div>
      <!-- FR-04：只关「仅告知」（转存成功摘要）。需处理级永远发，这里不给开关——
           关掉静音却漏掉链接失效/Cookie 过期，比吵一点糟得多。 -->
      <div class="f">
        <label class="field-label">转存成功时通知我</label>
        <el-switch v-model="draft.notify_info" />
        <small class="hint">关闭后不再推送这个任务的转存成功摘要；链接失效、账号过期等需处理消息照旧推送。</small>
      </div>
      <div class="f">
        <label class="field-label">执行方式</label>
        <el-radio-group v-model="draft.run_mode">
          <el-radio-button v-for="o in RUN_MODE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</el-radio-button>
        </el-radio-group>
        <small class="hint">{{ RUN_MODE_OPTIONS.find((o) => o.value === draft.run_mode)?.hint }}</small>
        <small class="hint">{{ disabledNote }}</small>
        <!-- 「停用」只在编辑态出现：新建任务没有"暂停"语义（要等资源放出用「一次性」，先存着不跑用「仅手动」），
             载荷里 disabled 恒为 false，后端字段照旧存在。 -->
        <div v-if="task" class="sub">
          <label class="field-label">停用</label>
          <el-switch v-model="draft.disabled" />
        </div>
      </div>

      <div v-if="isFollow" class="f f--wide">
        <label class="field-label">更新频率</label>
        <el-select v-model="scheduleMode">
          <el-option v-for="p in SCHEDULE_PRESETS" :key="p.value" :label="p.label" :value="p.value" />
        </el-select>
        <el-input
          v-if="scheduleMode === '__custom'"
          v-model="customCron"
          placeholder="标准 crontab，如 */5 17-23 * * *"
          style="margin-top: 8px"
        />
        <div v-if="scheduleMode === '__custom' && !customCron.trim()" class="hint">
          请先填写 crontab 表达式，否则将回退为继承全局。
        </div>
        <div v-if="draft.schedule.startsWith('interval:') && Number(draft.schedule.split(':')[1]) < 5" class="hint">
          频率过高可能触发夸克风控，建议 ≥5 分钟。
        </div>
      </div>

      <div class="f f--wide">
        <label class="field-label">子目录也按集数/画质过滤</label>
        <el-switch v-model="subdirFilterOn" />
        <small class="hint">{{ SUBDIR_FILTER_HINT }}</small>
        <!-- 这句是 engine.py 的既有行为（子目录里的文件不参与魔法重命名），本轮没改引擎，不许写得像修好了。 -->
        <small class="hint">{{ subdirHint }}</small>
      </div>
    </div>

    <el-collapse v-model="advancedOpen" class="adv">
      <el-collapse-item title="高级设置（正则 / 魔法变量 / 子目录 / 截止日期等）" name="adv">
        <div class="grid">
          <div class="f">
            <label class="field-label">匹配正则 (pattern)</label>
            <el-input
              ref="patternInput"
              v-model="draft.pattern"
              data-field="pattern"
              placeholder="如 $TV 或 .*?.mp4"
              @focus="onFieldFocus"
            />
          </div>
          <div class="f">
            <label class="field-label">替换式 (replace)</label>
            <el-input
              v-model="draft.replace"
              data-field="replace"
              placeholder="如 {TASKNAME}-{E}"
              @focus="onFieldFocus"
            />
          </div>
          <div class="f f--magic">
            <label class="field-label">魔法变量（点击插入光标处）</label>
            <div class="vars">
              <button v-for="v in MAGIC_VARIABLES" :key="v" class="var" @click="insertVar(v)">
                {{ v }}
              </button>
              <el-button size="small" text @click="openSelector('preview')"> 预览正则效果 </el-button>
              <!-- FR-10：新手面对两个空框无从下手，给可一键套用的预设 -->
              <el-button size="small" text type="primary" @click="tplOpen = true"> 命名模板库 </el-button>
            </div>
          </div>

          <div class="f">
            <label class="field-label">忽略扩展名</label>
            <el-switch v-model="draft.ignore_extension" />
          </div>

          <div class="f">
            <label class="field-label">子目录追更正则 (update_subdir)</label>
            <el-input v-model="draft.update_subdir" placeholder="留空=不递归子目录" />
            <div class="hint">
              首屏那个开关就是这一条的开关位：留空 = 关、<code>.*</code> = 开；填别的正则是自定义递归范围，
              首屏的开关既不覆盖也不清空它。
            </div>
          </div>
          <div class="f">
            <label class="field-label">子目录重存模式（重存时集数/画质过滤不生效，整目录搬运）</label>
            <el-switch v-model="draft.update_subdir_resave" />
          </div>

          <div class="f">
            <label class="field-label">递归下载子目录</label>
            <el-switch v-model="draft.download_subdir" :disabled="!draft.auto_download" />
          </div>
          <div class="f f--wide">
            <label class="field-label">本地下载子目录 (download_savepath)</label>
            <el-input
              v-model="draft.download_savepath"
              :disabled="!draft.auto_download"
              placeholder="留空 = 在下载根目录下镜像网盘目录；填写如「剧集」则平铺到 下载根/剧集/文件名"
            />
          </div>

          <div v-if="isFollow" class="f">
            <label class="field-label">截止日期 (enddate)</label>
            <el-date-picker
              v-model="draft.enddate"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="不过期"
              style="width: 100%"
            />
          </div>
          <div class="f">
            <label class="field-label">指定账号</label>
            <el-select v-model="draft.account_id" clearable placeholder="自动选择">
              <el-option v-for="a in accounts" :key="a.id" :label="a.nickname || a.name || `#${a.id}`" :value="a.id" />
            </el-select>
          </div>
          <div v-if="draft.account_id" class="f">
            <label class="field-label">账号容灾</label>
            <el-switch v-model="draft.account_failover" />
            <p class="hint">
              开启：该账号失效 / 被风控 / 空间不足时，自动切到同网盘的其它可用账号继续跑（会通知你切到了哪个号）。<br />
              关闭：严格只用这个号，宁可本轮失败也不换。
            </p>
          </div>

          <div v-if="isFollow" class="f f--wide">
            <label class="field-label">按星期运行（不选 = 每天）</label>
            <div class="weeks">
              <button
                v-for="d in 7"
                :key="d"
                class="week"
                :class="{ on: draft.runweek.includes(d) }"
                @click="toggleWeek(d)"
              >
                {{ WEEK_LABELS[d - 1] }}
              </button>
            </div>
          </div>
        </div>
      </el-collapse-item>
    </el-collapse>

    <div class="actions">
      <el-button v-if="hasId" type="danger" plain @click="emit('remove', hasId as number)"> 删除 </el-button>
      <span class="spacer" />
      <el-button :disabled="dryRunning" @click="tryRun">
        {{ dryRunning ? "正在只读访问网盘…" : "试跑（只读：不转存、不下载）" }}
      </el-button>
      <el-button @click="emit('cancel')"> 取消 </el-button>
      <el-button type="primary" :disabled="hasId !== null && !isDirty" @click="submit"> 保存 </el-button>
    </div>

    <!-- 试跑结果：只在点按钮时才跑（每次都会真访问网盘，不做自动触发）；message 原样显示，
         后端说 banned 就显示 banned 的原文，计数键缺席时那几行整行不出现。
         aria-live 挂在这块上：结果是点完按钮异步回来的，读屏要能在它落地时读到那句话。 -->
    <template v-if="dryResult || dryError || dryRunning">
      <div class="dryrun" aria-live="polite">
        <small :class="dryMsgClass">{{ dryMessage }}</small>
        <small class="dryrun__body">{{ dryBody }}</small>
      </div>
    </template>

    <FileSelector
      v-model="selector"
      :mode="selectorMode"
      :shareurl="draft.shareurl"
      :taskname="draft.taskname"
      :pattern="draft.pattern"
      :replace="draft.replace"
      :ignore-extension="draft.ignore_extension"
      :update-subdir="draft.update_subdir"
      :savepath="draft.savepath"
      @confirm="onSelectorConfirm"
    />

    <NameTemplatePicker v-model="tplOpen" :taskname="draft.taskname" @apply="applyNameTemplate" />
  </div>
</template>

<style scoped>
.form {
  padding: 16px;
  background: #fbfcfe;
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-top: 10px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 14px;
}
/* 同路径提示条（复制为新任务时出现）：警示底色，逐字文案见 spec 4.3。 */
.form > p {
  margin: 0 0 10px;
}
.dup-path {
  display: block;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-left: 3px solid #d97706;
  border-radius: 8px;
  background: #fffbeb;
  color: #92400e;
  font-size: 13px;
}
.adv {
  margin-top: 6px;
  border-top: none;
}
.adv :deep(.el-collapse-item__content) {
  padding-bottom: 14px;
}
.f {
  min-width: 0;
}
.f--wide {
  grid-column: 1 / -1;
}
.row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.sub {
  margin-top: 10px;
}
/* 说明文字一律用 small 承载并按块显示；下拉宽度收进 CSS，模板里不写 style 属性。 */
small.hint {
  display: block;
}
.f :deep(.el-select) {
  width: 100%;
}
.link {
  color: var(--primary);
  cursor: pointer;
  font-size: 13px;
}
.link--danger {
  color: var(--danger);
}
.row :deep(.el-input) {
  flex: 1;
}
.vars {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.var {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 6px;
  padding: 3px 8px;
  font-size: 12px;
  cursor: pointer;
  font-family: monospace;
  color: var(--primary);
}
.var:hover {
  background: var(--primary-soft);
}
.hint {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: #8b94a7;
}
.hint code {
  background: #eef1f6;
  border-radius: 4px;
  padding: 0 4px;
  font-family: monospace;
}
/* 抓取范围那一行：两档互斥 + 旁边的动作项，装在一个自适应的横排里。 */
.capture {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}
/* 「自己写正则」是个动作而不是选项，长得像链接即可；样式走父级选择器，元素本身只留一个属性。 */
.capture > span {
  border: none;
  background: none;
  padding: 0;
  font-size: 13px;
  color: var(--primary);
  cursor: pointer;
}
/* 自定义值态的如实说明：两档都没被选中时点名当前值住在高级设置那一条里。 */
.capture > small {
  font-size: 12px;
  color: #8b94a7;
}
/* 抓取范围那档拿到的 $TV 展开式：整行等宽原文显示，读得完；高级区那条 <code>.*</code> 仍按行内排。 */
small.hint > code {
  display: block;
  margin-top: 4px;
  padding: 2px 6px;
  word-break: break-all;
}
.weeks {
  display: flex;
  gap: 8px;
}
.week {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  border: 1px solid var(--border);
  background: #fff;
  cursor: pointer;
  font-size: 13px;
  color: #5b6270;
}
.week.on {
  background: var(--primary);
  color: #fff;
  border-color: var(--primary);
}
.actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 16px;
}
.spacer {
  flex: 1;
}
.dryrun {
  margin-top: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
  padding: 10px 12px;
}
.dryrun__msg {
  display: block;
  font-size: 13px;
  margin: 0;
}
.dryrun__msg.err {
  color: var(--danger);
}
/* 汇总与前 20 条由脚本拼成多行文本，这里按 pre-line 原样换行显示。 */
.dryrun__body {
  display: block;
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-line;
  color: #5b6270;
}
@media (max-width: 640px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
