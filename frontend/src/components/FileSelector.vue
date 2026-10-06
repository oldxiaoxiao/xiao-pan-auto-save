<script setup lang="ts">
import { ref, watch, computed } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api/client";
import type { FsItem } from "../api/types";
import { formatSize, formatTime } from "../utils";

type Mode = "savepath" | "startfid" | "preview";

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    mode: Mode;
    shareurl?: string;
    taskname?: string;
    pattern?: string;
    replace?: string;
    ignoreExtension?: boolean;
    updateSubdir?: string;
    savepath?: string;
    driver?: string;
  }>(),
  {
    shareurl: "",
    taskname: "",
    pattern: "",
    replace: "",
    ignoreExtension: false,
    updateSubdir: "",
    savepath: "",
    driver: "quark",
  },
);

const emit = defineEmits<{
  (e: "update:modelValue", v: boolean): void;
  (e: "confirm", payload: Record<string, string>): void;
}>();

// —— 上半区：分享预览 ——
const previewItems = ref<FsItem[]>([]);
const previewLoading = ref(false);
const previewError = ref("");
const previewPath = ref("");
const showPreview = computed(() => (!!props.shareurl && props.mode !== "savepath" ? true : !!props.shareurl));

const previewCrumbs = computed(() => {
  const parts = previewPath.value.split("/").filter(Boolean);
  return parts.map((p, i) => ({ label: p, path: "/" + parts.slice(0, i + 1).join("/") }));
});

async function loadPreview() {
  if (!props.shareurl) return;
  previewLoading.value = true;
  previewError.value = "";
  try {
    const r = await api.sharePreview({
      shareurl: props.shareurl,
      path: previewPath.value,
      taskname: props.taskname,
      pattern: props.pattern,
      replace: props.replace,
      ignore_extension: props.ignoreExtension,
      update_subdir: props.updateSubdir,
      savepath: props.savepath,
    });
    previewItems.value = r.list;
    if (!r.ok) previewError.value = r.message || "分享不可用";
  } catch (e) {
    previewError.value = (e as Error).message;
  } finally {
    previewLoading.value = false;
  }
}

function enterPreviewDir(item: FsItem) {
  if (!item.is_dir) return;
  previewPath.value = previewPath.value ? `${previewPath.value}/${item.name}` : `/${item.name}`;
  loadPreview();
}

function gotoPreviewPath(path: string) {
  previewPath.value = path;
  loadPreview();
}

// —— 下半区：目标目录浏览 ——
const browsePath = ref("/");
const dirItems = ref<FsItem[]>([]);
const dirLoading = ref(false);
const dirError = ref("");
const atRoot = ref(true);
const sortKey = ref<"name" | "size" | "mtime">("name");
const sortAsc = ref(true);

const crumbs = computed(() => {
  const parts = browsePath.value.split("/").filter(Boolean);
  return parts.map((p, i) => ({ label: p, path: "/" + parts.slice(0, i + 1).join("/") }));
});

const sortedDirs = computed(() => {
  const arr = [...dirItems.value];
  arr.sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let r = 0;
    if (sortKey.value === "size") r = a.size - b.size;
    else if (sortKey.value === "mtime") r = String(a.mtime).localeCompare(String(b.mtime));
    else r = a.name.localeCompare(b.name, "zh");
    return sortAsc.value ? r : -r;
  });
  return arr;
});

function joinPath(base: string, name: string): string {
  return `${base.replace(/\/$/, "")}/${name}`;
}

async function loadDir(path: string) {
  dirLoading.value = true;
  dirError.value = "";
  try {
    const r = await api.listDir(path || "/", props.driver);
    browsePath.value = r.path;
    dirItems.value = r.list;
    atRoot.value = r.at_root;
  } catch (e) {
    dirError.value = (e as Error).message;
    dirItems.value = [];
  } finally {
    dirLoading.value = false;
  }
}

function enter(item: FsItem) {
  if (item.is_dir) loadDir(joinPath(browsePath.value, item.name));
}

async function renameItem(item: FsItem) {
  try {
    const { value } = await ElMessageBox.prompt("新的名称", "重命名", { inputValue: item.name });
    if (!value || value === item.name) return;
    const full = joinPath(browsePath.value, item.name);
    await api.renameFile(full, value, props.driver);
    ElMessage.success("已重命名");
    loadDir(browsePath.value);
  } catch {
    /* 用户取消 */
  }
}

async function deleteItem(item: FsItem) {
  const full = joinPath(browsePath.value, item.name);
  try {
    await ElMessageBox.confirm(`确定删除「${item.name}」？${item.is_dir ? "（含目录内全部内容）" : ""}`, "删除确认", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    let purge = false;
    try {
      await ElMessageBox.confirm("是否彻底删除（不进回收站）？取消 = 移入回收站", "删除方式", {
        confirmButtonText: "彻底删除",
        cancelButtonText: "回收站",
        type: "warning",
      });
      purge = true;
    } catch {
      purge = false;
    }
    await api.deleteFile(full, purge, props.driver);
    ElMessage.success(purge ? "已彻底删除" : "已移入回收站");
    loadDir(browsePath.value);
  } catch (e) {
    if (e !== "cancel" && e instanceof Error) ElMessage.error(e.message);
  }
}

function confirmSavepath() {
  emit("confirm", { path: browsePath.value });
}

function pickStartFid(item: FsItem) {
  emit("confirm", { fid: item.fid, name: item.name });
}

/** 行内「起点」：把目录本身设为起始点（engine 遍历到这个 fid 就截断，整目录连同更新的都会转）。 */
function pickDirAsStart(item: FsItem) {
  pickStartFid(item);
  close();
}

function onRowClick(item: FsItem) {
  if (props.mode === "startfid") {
    // 目录要先钻进去：分享按季/按季分目录是常态，而原来点任何一行都直接选中关窗，
    // 结果起点只能落在分享最外层——里面那句「点击 📁 目录进入下一层」当场就是谎。
    // 整目录当起点这个真需求没被抹掉，挪给行内「起点」那一条。
    if (item.is_dir) enterPreviewDir(item);
    else {
      pickStartFid(item);
      close();
    }
  } else if (props.mode === "savepath") {
    enter(item);
  } else if (props.mode === "preview") {
    enterPreviewDir(item);
  }
}

function close() {
  emit("update:modelValue", false);
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return;
    previewItems.value = [];
    previewPath.value = "";
    loadPreview();
    if (props.mode === "savepath") {
      loadDir(props.savepath || "/");
    }
  },
  { immediate: true },
);
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="mode === 'startfid' ? '选择起始文件' : mode === 'preview' ? '浏览分享 / 正则效果' : '选择保存目录'"
    width="min(900px, 94vw)"
    top="6vh"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <!-- 上半区：分享预览 -->
    <section v-if="showPreview" class="pane">
      <div class="pane__head">
        <span>分享文件预览（含正则效果）</span>
        <el-button size="small" text @click="loadPreview"> 刷新 </el-button>
      </div>
      <div class="crumb">
        <a class="crumb__link" :class="{ active: !previewPath }" @click="gotoPreviewPath('')">分享根目录</a>
        <template v-for="c in previewCrumbs" :key="c.path">
          <span class="crumb__sep">/</span>
          <a class="crumb__link" @click="gotoPreviewPath(c.path)">{{ c.label }}</a>
        </template>
        <span class="crumb__hint text-muted">点击 📁 目录进入下一层</span>
      </div>
      <div v-if="previewError" class="pane__err">
        {{ previewError }}
      </div>
      <el-table
        v-if="previewItems.length"
        v-loading="previewLoading"
        :data="previewItems"
        size="small"
        max-height="220"
        @row-click="onRowClick"
      >
        <el-table-column label="原文件名">
          <template #default="{ row }">
            <span class="fn">{{ row.is_dir ? "📁" : "📄" }} {{ row.name }}</span>
            <a v-if="mode === 'startfid' && row.is_dir" class="dir-pick text-muted" @click.stop="pickDirAsStart(row)">
              起点
            </a>
          </template>
        </el-table-column>
        <el-table-column label="处理后">
          <template #default="{ row }">
            <span v-if="row.name_re" class="name-re">{{ row.name_re }}</span>
            <span v-if="row.saved_as" class="text-muted">已存在: {{ row.saved_as }}</span>
            <span v-if="!row.name_re" class="text-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="90">
          <template #default="{ row }">
            {{ formatSize(row.size) }}
          </template>
        </el-table-column>
        <el-table-column label="时间" width="150">
          <template #default="{ row }">
            {{ formatTime(row.mtime) }}
          </template>
        </el-table-column>
      </el-table>
      <div v-else-if="!previewLoading" class="empty-hint">未获取到分享文件，或链接不可用</div>
    </section>

    <!-- 下半区：目标目录浏览 -->
    <section v-if="mode === 'savepath'" class="pane">
      <div class="pane__head">
        <span>目标目录</span>
      </div>
      <div class="crumb">
        <a class="crumb__link" :class="{ active: atRoot }" @click="loadDir('/')">夸克网盘</a>
        <template v-for="c in crumbs" :key="c.path">
          <span class="crumb__sep">/</span>
          <a class="crumb__link" @click="loadDir(c.path)">{{ c.label }}</a>
        </template>
      </div>
      <div class="toolbar">
        <el-input v-model="browsePath" size="small" placeholder="路径，如 /动漫" @keyup.enter="loadDir(browsePath)" />
        <el-button size="small" @click="loadDir(browsePath)"> 前往 </el-button>
        <el-select v-model="sortKey" size="small" style="width: 100px">
          <el-option label="名称" value="name" />
          <el-option label="大小" value="size" />
          <el-option label="时间" value="mtime" />
        </el-select>
        <el-button size="small" @click="sortAsc = !sortAsc">
          {{ sortAsc ? "↑" : "↓" }}
        </el-button>
      </div>
      <div v-if="dirError" class="pane__err">
        {{ dirError }}
      </div>
      <el-table v-loading="dirLoading" :data="sortedDirs" size="small" max-height="260" @row-dblclick="enter">
        <el-table-column label="名称">
          <template #default="{ row }">
            <a class="fn dir-link" :class="{ 'is-dir': row.is_dir }" @click="enter(row)">
              {{ row.is_dir ? "📁" : "📄" }} {{ row.name }}
            </a>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="90">
          <template #default="{ row }">
            {{ formatSize(row.size) }}
          </template>
        </el-table-column>
        <el-table-column label="时间" width="150">
          <template #default="{ row }">
            {{ formatTime(row.mtime) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button size="small" text @click.stop="renameItem(row)"> 重命名 </el-button>
            <el-button size="small" text type="danger" @click.stop="deleteItem(row)"> 删除 </el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <template #footer>
      <el-button @click="close"> 关闭 </el-button>
      <el-button v-if="mode === 'savepath'" type="primary" @click="confirmSavepath"> 选择此目录 </el-button>
      <p v-if="mode === 'startfid'" class="hint text-muted">
        点击 📄 设为「起始文件」；点 📁 会进入该目录，要把整个目录当起点就点行内的「起点」
      </p>
    </template>
  </el-dialog>
</template>

<style scoped>
.pane {
  margin-bottom: 18px;
}
.pane__head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 8px;
}
.pane__err {
  color: var(--danger);
  font-size: 13px;
  margin-bottom: 8px;
}
.fn {
  font-size: 13px;
}
.dir-link {
  cursor: pointer;
}
.dir-link.is-dir:hover {
  color: var(--primary);
}
/* 起点选择态里目录行尾的那条「起点」：整目录当起点走它，点行本身是钻进目录。 */
.dir-pick {
  margin-left: 8px;
  font-size: 12px;
  cursor: pointer;
}
.dir-pick:hover {
  color: var(--primary);
}
.name-re {
  color: var(--primary);
  font-weight: 600;
}
.empty-hint {
  color: var(--text-muted);
  font-size: 13px;
  padding: 12px 0;
}
.crumb {
  font-size: 13px;
  margin-bottom: 8px;
  color: var(--text-muted);
}
.crumb__link {
  cursor: pointer;
  color: var(--primary);
}
.crumb__sep {
  margin: 0 4px;
}
.crumb__hint {
  margin-left: 10px;
  font-size: 12px;
}
.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.hint {
  font-size: 12px;
  margin-top: 8px;
}
</style>
