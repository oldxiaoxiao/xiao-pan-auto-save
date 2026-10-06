<script setup lang="ts">
import { reactive, ref, watch, computed } from "vue";
import { ElMessage } from "element-plus";
import FileSelector from "./FileSelector.vue";
import SearchSuggest from "./SearchSuggest.vue";
import type { Account, RunMode, Task, TaskPayload } from "../api/types";
import { MAGIC_VARIABLES } from "../constants";
import { WEEK_LABELS } from "../utils";

const QUALITY_OPTIONS = ["4K", "2160P", "1080P", "720P", "x265", "HDR"];

/** 执行方式：决定「要不要自动跑」，与「停用」（临时暂停一切）分工不同。 */
const RUN_MODE_OPTIONS: { value: RunMode; label: string; hint: string }[] = [
  { value: "follow", label: "定时追更", hint: "按更新频率/全局调度自动追更" },
  { value: "manual", label: "仅手动", hint: "永不自动跑，只在点「运行」时执行" },
  {
    value: "once",
    label: "一次性",
    hint: "自动重试到拿到为止；资源没放出不算失败、每天再看一次；连续三次真失败则停摆，点「运行」重新开启",
  },
];

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

function blank(): TaskPayload & { startfid_name: string } {
  return {
    taskname: "",
    shareurl: "",
    savepath: "/",
    pattern: "",
    replace: "",
    ignore_extension: false,
    startfid: "",
    startfid_name: "",
    update_subdir: "",
    update_subdir_resave: false,
    enddate: "",
    runweek: [],
    auto_download: true,
    run_mode: "follow" as RunMode,
    download_subdir: false,
    download_savepath: "",
    disabled: false,
    account_id: null,
    sort_order: 0,
    episode_start: 0,
    episode_end: 0,
    quality: "",
    schedule: "",
  };
}

const draft = reactive(blank());
let original = "";

// draft.quality 以逗号分隔存储；UI 用数组双向映射
const qualityList = computed({
  get: () => (draft.quality ? draft.quality.split(",").filter(Boolean) : []),
  set: (v: string[]) => (draft.quality = v.join(",")),
});
const advancedOpen = ref<string[]>([]); // 高级区默认收起

// 只有「定时追更」才需要配置频率/星期/截止日期；其余形态收起来，但 draft.schedule 等值按设计保留不清空。
const isFollow = computed(() => draft.run_mode === "follow");

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
  Object.assign(draft, blank(), task ? { ...task, startfid_name: "" } : {});
  if (!task && props.prefill) Object.assign(draft, props.prefill);
  draft.runweek = [...(task?.runweek ?? [])];
  // 若已有任务的 schedule 是不在预设里的 cron,回填自定义输入框
  const s = task?.schedule ?? "";
  if (s.startsWith("cron:") && !SCHEDULE_PRESETS.some((p) => p.value === s)) customCron.value = s.slice(5);
  else customCron.value = "";
  original = snapshot();
}

function snapshot(): string {
  const { startfid_name, ...rest } = draft;
  void startfid_name;
  return JSON.stringify(rest);
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

function submit() {
  if (!draft.taskname.trim()) return ElMessage.warning("请填写任务名称");
  if (!draft.shareurl.trim()) return ElMessage.warning("请填写分享链接");
  if (!draft.savepath.trim()) return ElMessage.warning("请选择保存路径");
  const { startfid_name, ...payload } = draft;
  void startfid_name;
  emit("save", { ...payload });
}

const hasId = computed(() => props.task?.id ?? null);
</script>

<template>
  <div class="form">
    <div class="grid">
      <div class="f f--wide">
        <label class="field-label">任务名称 / 智能搜索</label>
        <SearchSuggest v-model:taskname="draft.taskname" v-model:shareurl="draft.shareurl" />
      </div>
      <div class="f f--wide">
        <label class="field-label">分享链接</label>
        <div class="row">
          <el-input v-model="draft.shareurl" placeholder="https://pan.quark.cn/s/..." />
          <el-button :icon="'🔍'" :disabled="!draft.shareurl.trim()" @click="openSelector('preview')">
            浏览
          </el-button>
        </div>
      </div>
      <div class="f f--wide">
        <label class="field-label">保存路径</label>
        <div class="row">
          <el-input v-model="draft.savepath" placeholder="/动漫" />
          <el-button :icon="'📁'" @click="openSelector('savepath')"> 选择 </el-button>
        </div>
      </div>

      <div class="f">
        <label class="field-label">起始集（含，0=不限）</label>
        <el-input-number v-model="draft.episode_start" :min="0" :max="9999" controls-position="right" />
      </div>
      <div class="f">
        <label class="field-label">结束集（含，0=不限）</label>
        <el-input-number v-model="draft.episode_end" :min="0" :max="9999" controls-position="right" />
      </div>
      <div class="f f--wide">
        <label class="field-label">画质（多选，留空=不限；4K 与 2160P 互为别名）</label>
        <el-select v-model="qualityList" multiple clearable placeholder="不限画质" style="width: 100%">
          <el-option v-for="q in QUALITY_OPTIONS" :key="q" :label="q" :value="q" />
        </el-select>
        <div class="hint">
          提示：集数/画质只过滤文件，不作用于子目录；若分享里有整目录（如 001-184 合集），请在高级设置里填「匹配正则」（如
          <code>$TV</code>）以排除非剧集目录。
        </div>
      </div>
      <div class="f">
        <label class="field-label">下载到本地</label>
        <el-switch v-model="draft.auto_download" />
      </div>
      <div class="f f--wide">
        <label class="field-label">执行方式</label>
        <el-radio-group v-model="draft.run_mode">
          <el-radio-button v-for="o in RUN_MODE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</el-radio-button>
        </el-radio-group>
        <div class="hint">{{ RUN_MODE_OPTIONS.find((o) => o.value === draft.run_mode)?.hint }}</div>
        <div v-if="!isFollow && draft.disabled" class="hint">
          该行当前为停用状态；改回「定时追更」后需手动取消停用才会恢复自动运行。
        </div>
      </div>
      <div v-if="isFollow" class="f f--wide">
        <label class="field-label">更新频率</label>
        <el-select v-model="scheduleMode" style="width: 100%">
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
    </div>

    <el-collapse v-model="advancedOpen" class="adv">
      <el-collapse-item title="高级设置（正则 / 魔法变量 / 子目录 / 截止日期等）" name="adv">
        <div class="grid">
          <div class="f">
            <label class="field-label">匹配正则 (pattern)</label>
            <el-input
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
            </div>
          </div>

          <div class="f">
            <label class="field-label">忽略扩展名</label>
            <el-switch v-model="draft.ignore_extension" />
          </div>
          <div class="f">
            <label class="field-label">起始文件 (startfid)</label>
            <div class="row">
              <el-input :model-value="draft.startfid_name || draft.startfid" placeholder="未选择" readonly>
                <template #append>
                  <el-button @click="openSelector('startfid')"> 选择 </el-button>
                </template>
              </el-input>
              <el-button v-if="draft.startfid" text type="danger" @click="clearStart"> 清除 </el-button>
            </div>
          </div>

          <div class="f">
            <label class="field-label">子目录追更正则 (update_subdir)</label>
            <el-input v-model="draft.update_subdir" placeholder="留空=不递归子目录" />
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
            <el-select v-model="draft.account_id" clearable placeholder="自动选择" style="width: 100%">
              <el-option v-for="a in accounts" :key="a.id" :label="a.nickname || a.name || `#${a.id}`" :value="a.id" />
            </el-select>
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
          <div class="f">
            <label class="field-label">停用</label>
            <el-switch v-model="draft.disabled" />
          </div>
        </div>
      </el-collapse-item>
    </el-collapse>

    <div class="actions">
      <el-button v-if="hasId" type="danger" plain @click="emit('remove', hasId as number)"> 删除 </el-button>
      <span class="spacer" />
      <el-button @click="emit('cancel')"> 取消 </el-button>
      <el-button type="primary" :disabled="hasId !== null && !isDirty" @click="submit"> 保存 </el-button>
    </div>

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
@media (max-width: 640px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
