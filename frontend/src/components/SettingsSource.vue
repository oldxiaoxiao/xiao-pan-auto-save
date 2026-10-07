<script setup lang="ts">
import { reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api/client";
import { useSettingsStore } from "../stores/settings";
import type { EngineTypeSpec, SearchEngine } from "../api/types";

const store = useSettingsStore();
const saving = ref(false);
const types = ref<EngineTypeSpec[]>([]);
const engines = reactive<SearchEngine[]>([]);

api
  .engineTypes()
  .then((r) => (types.value = r.data))
  .catch(() => ElMessage.error("搜索协议清单读取失败，请刷新页面"));

watch(
  // settings 未到齐时后端给的是 undefined，别把用户刚填的行冲掉
  () => store.settings?.source,
  (v) => {
    if (!v?.engines) return;
    engines.splice(0, engines.length, ...v.engines.map((e) => ({ ...e })));
  },
  { immediate: true, deep: true },
);

function specOf(type: string): EngineTypeSpec | undefined {
  return types.value.find((t) => t.type === type);
}

function newId(): string {
  return `e${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

function addEngine() {
  const spec = types.value[0];
  engines.push({
    id: newId(),
    type: spec?.type ?? "pansou",
    name: `${spec?.label ?? "引擎"} ${engines.length + 1}`,
    server: spec?.default_server ?? "",
    enable: true,
  });
}

function onTypeChange(engine: SearchEngine) {
  const spec = specOf(engine.type);
  if (!spec) return;
  // 换协议：上一个协议专有的字段（用户名/密码/token 等）摘干净，别留脏配置
  const keep = new Set(["id", "type", "name", "enable", "server", ...spec.fields.map((f) => f.key)]);
  const next: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(engine)) {
    if (keep.has(key)) next[key] = value;
  }
  for (const field of spec.fields) {
    if (next[field.key] === undefined) next[field.key] = "";
  }
  if (!next.server) next.server = spec.default_server;
  for (const key of Object.keys(engine)) delete engine[key];
  Object.assign(engine, next);
}

function fieldText(engine: SearchEngine, key: string): string {
  return String(engine[key] ?? "");
}

function setField(engine: SearchEngine, key: string, value: string) {
  engine[key] = value;
}

function fieldPlaceholder(engine: SearchEngine, key: string): string {
  if (key === "token") return "留空，登录后自动写入";
  if (key !== "server") return "";
  const builtin = specOf(engine.type)?.default_server ?? "";
  return builtin ? `留空=用内置地址 ${builtin}` : "必填，自建服务地址";
}

function removeEngine(index: number) {
  engines.splice(index, 1);
}

function missingRequired(engine: SearchEngine): string {
  const spec = specOf(engine.type);
  if (!spec) return "";
  const hit = spec.fields.find((f) => f.required && !String(engine[f.key] ?? "").trim());
  return hit ? hit.label : "";
}

async function save() {
  for (const [i, engine] of engines.entries()) {
    const missing = missingRequired(engine);
    if (missing) {
      ElMessage.error(`第 ${i + 1} 项「${engine.name || engine.type}」缺少${missing}`);
      return;
    }
  }
  saving.value = true;
  const payload = {
    engines: engines.map((e) => ({
      ...e,
      name: e.name.trim() || specOf(e.type)?.label || e.type,
      server: e.server.trim(),
    })),
  };
  try {
    await store.save("source", payload);
    ElMessage.success("搜索引擎已保存");
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="pane">
    <div class="head">
      <h3>资源搜索引擎</h3>
      <el-button type="primary" size="small" :loading="saving" @click="save"> 保存 </el-button>
    </div>

    <p class="tip text-muted">
      同一种协议可以配多个实例互为备份；建任务时默认把所有启用的引擎一起搜，重复的分享链接会合并成一条。
    </p>

    <div v-for="(engine, i) in engines" :key="engine.id" class="grp card">
      <div class="row">
        <el-input v-model="engine.name" class="name" placeholder="引擎名称（如 公共站 / 家里自建）" />
        <el-select v-model="engine.type" class="type" @change="onTypeChange(engine)">
          <el-option v-for="t in types" :key="t.type" :label="t.label" :value="t.type" />
          <el-option v-if="!specOf(engine.type)" :label="`${engine.type}（本版本不支持）`" :value="engine.type" />
        </el-select>
        <el-switch v-model="engine.enable" size="small" active-text="启用" />
        <el-button size="small" text type="danger" @click="removeEngine(i)"> 删除 </el-button>
      </div>

      <div v-if="!specOf(engine.type)" class="warn">
        本版本还不认识协议 <code>{{ engine.type }}</code
        >，会跳过它进行搜索；配置里的字段原样保留，升级后即可用。
      </div>

      <div class="two">
        <div v-for="field in specOf(engine.type)?.fields ?? []" :key="field.key">
          <label class="field-label">{{ field.label }}</label>
          <el-input
            :model-value="fieldText(engine, field.key)"
            :type="field.secret ? 'password' : 'text'"
            :show-password="field.secret"
            @update:model-value="(v: string) => setField(engine, field.key, String(v))"
            :placeholder="fieldPlaceholder(engine, field.key)"
          />
        </div>
      </div>
    </div>

    <el-button size="small" :disabled="!types.length" @click="addEngine"> 新增引擎 </el-button>
  </div>
</template>

<style scoped>
.pane {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.head h3 {
  margin: 0;
}
.tip {
  margin: 0;
  font-size: 12px;
}
.grp {
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.name {
  max-width: 240px;
}
.type {
  width: 180px;
}
.two {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
}
.warn {
  font-size: 12px;
  color: var(--warn);
}
</style>
