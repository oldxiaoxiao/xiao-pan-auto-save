<script setup lang="ts">
import { ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { useSettingsStore } from "../stores/settings";

const store = useSettingsStore();
const text = ref("");
const err = ref("");
const saving = ref(false);

watch(
  () => store.settings.magic_regex,
  (v) => {
    text.value = JSON.stringify(v ?? {}, null, 2);
  },
  { immediate: true, deep: true },
);

function validate() {
  err.value = "";
  if (!text.value.trim()) return null;
  try {
    return JSON.parse(text.value);
  } catch (e) {
    err.value = "JSON 解析失败：" + (e as Error).message;
    return undefined;
  }
}

async function save() {
  const parsed = validate();
  if (parsed === undefined) return;
  saving.value = true;
  try {
    await store.save("magic_regex", parsed ?? {});
    ElMessage.success("魔法匹配规则已保存");
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    saving.value = false;
  }
}

function format() {
  const parsed = validate();
  if (parsed !== undefined) text.value = JSON.stringify(parsed ?? {}, null, 2);
}
</script>

<template>
  <div class="pane">
    <div class="head">
      <h3>魔法匹配 (magic_regex)</h3>
      <div class="actions">
        <el-button size="small" @click="format"> 格式化 </el-button>
        <el-button type="primary" size="small" :loading="saving" @click="save"> 保存 </el-button>
      </div>
    </div>
    <p class="text-muted">
      自定义 <code>{关键字: {pattern, replace}}</code>，任务正则命中关键字时展开为预设正则。示例：
      <code>{"$MOV": {"pattern": ".*", "replace": "{TASKNAME}"}}</code>
    </p>
    <el-input v-model="text" type="textarea" :rows="14" class="mono" @blur="validate" />
    <div v-if="err" class="err">
      {{ err }}
    </div>
  </div>
</template>

<style scoped>
.pane {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.head h3 {
  margin: 0;
}
.actions {
  display: flex;
  gap: 8px;
}
code {
  background: #f0f2f5;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 12px;
}
.err {
  color: var(--danger);
  font-size: 13px;
}
</style>
