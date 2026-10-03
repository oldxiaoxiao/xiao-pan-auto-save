<script setup lang="ts">
import { reactive, ref, watch, computed } from "vue";
import { ElMessage } from "element-plus";
import FileSelector from "./FileSelector.vue";
import SearchSuggest from "./SearchSuggest.vue";
import type { Account, Task, TaskPayload } from "../api/types";
import { MAGIC_VARIABLES } from "../constants";
import { WEEK_LABELS } from "../utils";

const QUALITY_OPTIONS = ["4K", "1080P", "720P", "x265", "HDR"];

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
    auto_download: false,
    download_subdir: false,
    download_savepath: "",
    disabled: false,
    account_id: null,
    sort_order: 0,
    episode_start: 0,
    episode_end: 0,
    quality: "",
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

function loadFrom(task: Task | null) {
  Object.assign(draft, blank(), task ? { ...task, startfid_name: "" } : {});
  if (!task && props.prefill) Object.assign(draft, props.prefill);
  draft.runweek = [...(task?.runweek ?? [])];
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
        <el-input v-model="draft.shareurl" placeholder="https://pan.quark.cn/s/..." />
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
        <label class="field-label">画质（多选，留空=不限）</label>
        <el-select v-model="qualityList" multiple clearable placeholder="不限画质" style="width: 100%">
          <el-option v-for="q in QUALITY_OPTIONS" :key="q" :label="q" :value="q" />
        </el-select>
      </div>
      <div class="f">
        <label class="field-label">下载到本地</label>
        <el-switch v-model="draft.auto_download" />
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
            <label class="field-label">子目录重存模式</label>
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

          <div class="f">
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

          <div class="f f--wide">
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
