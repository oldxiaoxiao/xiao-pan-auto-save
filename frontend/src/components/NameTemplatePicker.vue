<script setup lang="ts">
/** FR-10 命名模板库：一键套用预设，套用前就能看到自己的文件会被改成什么样。 */
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api/client";
import type { NameTemplate, NameTemplatePreview } from "../api/types";

const props = defineProps<{ modelValue: boolean; taskname?: string }>();
const emit = defineEmits<{
  "update:modelValue": [boolean];
  apply: [pattern: string, replace: string];
}>();

const builtin = ref<NameTemplate[]>([]);
const custom = ref<NameTemplate[]>([]);
const loading = ref(false);

const activeId = ref("");
const previewRows = ref<NameTemplatePreview[]>([]);
const previewing = ref(false);

const saving = ref(false);
const saveName = ref("");

const active = computed<NameTemplate | null>(() => {
  const all = [...builtin.value, ...custom.value];
  return all.find((t) => t.id === activeId.value) || null;
});

async function load() {
  loading.value = true;
  try {
    const r = await api.nameTemplates();
    builtin.value = r.data.builtin;
    custom.value = r.data.custom;
    if (!activeId.value && r.data.builtin.length) activeId.value = r.data.builtin[0].id;
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    loading.value = false;
  }
}

async function runPreview() {
  const t = active.value;
  if (!t) return;
  previewing.value = true;
  try {
    const r = await api.previewNameTemplate(t.pattern, t.replace, t.samples || [], props.taskname || "");
    previewRows.value = r.data;
  } catch (e) {
    ElMessage.error((e as Error).message);
    previewRows.value = [];
  } finally {
    previewing.value = false;
  }
}

watch(
  () => [props.modelValue, activeId.value],
  ([open]) => {
    if (open) void load().then(runPreview);
  },
  { immediate: true },
);
watch(activeId, () => void runPreview());

function useTemplate() {
  const t = active.value;
  if (!t) return;
  emit("apply", t.pattern, t.replace);
  emit("update:modelValue", false);
  ElMessage.success(`已套用「${t.name}」，记得先试跑确认效果`);
}

async function saveAsTemplate() {
  const t = active.value;
  if (!t) return;
  if (!saveName.value.trim()) {
    ElMessage.warning("请给模板起个名字");
    return;
  }
  saving.value = true;
  try {
    const r = await api.saveNameTemplate({
      name: saveName.value.trim(),
      pattern: t.pattern,
      replace: t.replace,
      desc: t.desc,
    });
    custom.value = r.data;
    saveName.value = "";
    ElMessage.success("已保存为我的模板");
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    saving.value = false;
  }
}

async function removeTemplate(id: string) {
  try {
    const r = await api.deleteNameTemplate(id);
    custom.value = r.data;
    if (activeId.value === id) activeId.value = builtin.value[0]?.id || "";
    ElMessage.success("已删除");
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="命名模板库"
    width="760px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div v-loading="loading" class="wrap">
      <aside class="list">
        <div class="list__group">内置模板</div>
        <button
          v-for="t in builtin"
          :key="t.id"
          class="item"
          :class="{ on: activeId === t.id }"
          @click="activeId = t.id"
        >
          {{ t.name }}
        </button>
        <div class="list__group" style="margin-top: 10px">我的模板</div>
        <div v-if="!custom.length" class="empty">还没有保存过模板</div>
        <div v-for="t in custom" :key="t.id" class="row">
          <button class="item item--custom" :class="{ on: activeId === t.id }" @click="activeId = t.id">
            {{ t.name }}
          </button>
          <button class="del" title="删除" @click="removeTemplate(t.id)">×</button>
        </div>
      </aside>

      <section class="detail">
        <template v-if="active">
          <h4>{{ active.name }}</h4>
          <p class="desc">{{ active.desc }}</p>
          <div class="code">
            <div><span class="k">匹配正则</span> <code>{{ active.pattern || "（留空 = 全部命中）" }}</code></div>
            <div><span class="k">替换式</span> <code>{{ active.replace }}</code></div>
          </div>

          <div class="preview">
            <div class="preview__head">
              示例效果
              <span class="text-muted">
                （用当前剧名「{{ taskname || "（未填）" }}」试算，与真实转存同源）
              </span>
            </div>
            <div v-loading="previewing">
              <div v-for="r in previewRows" :key="r.before" class="pr">
                <span class="pr__before">{{ r.before }}</span>
                <span class="pr__arrow">→</span>
                <span class="pr__after" :class="{ 'pr__after--skip': !r.matched }">
                  {{ r.matched ? r.after : "（正则未命中，不会转存）" }}
                </span>
                <span v-if="r.pending_index" class="tag">序号待定</span>
              </div>
              <div v-if="!previewRows.length && !previewing" class="empty">该模板没有自带示例</div>
            </div>
          </div>

          <div class="save">
            <el-input v-model="saveName" size="small" placeholder="存为我的模板：起个名字" style="max-width: 240px" />
            <el-button size="small" :loading="saving" @click="saveAsTemplate">保存</el-button>
          </div>
        </template>
        <div v-else class="empty">请选择左侧模板</div>
      </section>
    </div>

    <template #footer>
      <span class="text-muted">套用后请务必先试跑——预览算的是示例文件名，真实结果以试跑为准。</span>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :disabled="!active" @click="useTemplate">套用到本任务</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.wrap {
  display: grid;
  grid-template-columns: 190px 1fr;
  gap: 14px;
  min-height: 300px;
}
.list {
  border-right: 1px solid var(--border);
  padding-right: 10px;
}
.list__group {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 6px;
}
.item {
  display: block;
  width: 100%;
  text-align: left;
  border: none;
  background: none;
  padding: 7px 9px;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
  color: #333;
}
.item:hover {
  background: #f2f4f7;
}
.item.on {
  background: var(--primary-soft);
  color: var(--primary);
  font-weight: 600;
}
.item--custom {
  flex: 1;
}
.row {
  display: flex;
  align-items: center;
  gap: 2px;
}
.del {
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 16px;
  padding: 0 6px;
}
.del:hover {
  color: var(--danger);
}
.detail h4 {
  margin: 0 0 6px;
  font-size: 15px;
}
.desc {
  margin: 0 0 10px;
  font-size: 13px;
  color: #5b6270;
  line-height: 1.6;
}
.code {
  background: #f7f8fa;
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.9;
}
.code .k {
  color: var(--text-muted);
  display: inline-block;
  width: 62px;
}
.code code {
  background: #fff;
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
}
.preview {
  margin-top: 12px;
}
.preview__head {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
}
.pr {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  padding: 5px 0;
  border-top: 1px solid var(--border);
  flex-wrap: wrap;
}
.pr__before {
  color: var(--text-muted);
}
.pr__arrow {
  color: var(--text-muted);
}
.pr__after {
  font-weight: 600;
  color: var(--success);
}
.pr__after--skip {
  color: var(--danger);
  font-weight: 400;
}
.tag {
  font-size: 11px;
  background: #f2f4f7;
  color: var(--text-muted);
  border-radius: 8px;
  padding: 1px 7px;
}
.save {
  display: flex;
  gap: 8px;
  margin-top: 14px;
}
.empty {
  font-size: 12px;
  color: var(--text-muted);
  padding: 8px 0;
}
</style>
