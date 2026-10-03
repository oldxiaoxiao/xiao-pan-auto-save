<script setup lang="ts">
import { ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { useSettingsStore } from "../stores/settings";
import { api } from "../api/client";
import { NOTIFY_CHANNELS } from "../constants";
import type { NotifyTestResult, PushConfig } from "../api/types";

const store = useSettingsStore();
const cfg = ref<PushConfig>({});
const saving = ref(false);

watch(
  () => store.settings.push_config,
  (v) => {
    cfg.value = JSON.parse(JSON.stringify(v ?? {}));
  },
  { immediate: true, deep: true },
);

const results = ref<Record<string, NotifyTestResult[]>>({});
const testing = ref("");

function isOn(name: string, bool?: boolean): boolean {
  if (bool) return cfg.value.CONSOLE !== false && String(cfg.value.CONSOLE).toLowerCase() !== "false";
  const flag = cfg.value[`${name}_ENABLE`];
  if (flag === false || String(flag).toLowerCase() === "false") return false;
  return true;
}

function setOn(name: string, value: boolean, bool?: boolean) {
  if (bool) cfg.value.CONSOLE = value;
  else cfg.value[`${name}_ENABLE`] = value;
}

function getVal(key: string): string {
  const v = cfg.value[key];
  return v === undefined || v === null ? "" : String(v);
}
function setVal(key: string, val: string) {
  if (val === "") delete cfg.value[key];
  else cfg.value[key] = val;
}

async function save() {
  saving.value = true;
  try {
    await store.save("push_config", cfg.value);
    ElMessage.success("通知配置已保存");
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    saving.value = false;
  }
}

async function test(channel?: string) {
  testing.value = channel ?? "__all";
  try {
    const r = await api.notifyTest(channel);
    results.value[channel ?? "__all"] = r.results;
    if (!r.results.length) ElMessage.info("没有已启用的渠道");
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    testing.value = "";
  }
}
</script>

<template>
  <div class="pane">
    <div class="head">
      <h3>通知渠道</h3>
      <div class="actions">
        <el-button size="small" :loading="testing === '__all'" @click="test()"> 发送全部测试 </el-button>
        <el-button type="primary" size="small" :loading="saving" @click="save"> 保存 </el-button>
      </div>
    </div>
    <p class="text-muted">填写各渠道密钥即启用；关闭开关会写入 <code>*_ENABLE=false</code> 显式禁用。</p>

    <div v-for="ch in NOTIFY_CHANNELS" :key="ch.name" class="chan card">
      <div class="chan__top">
        <span class="chan__name">{{ ch.label }}</span>
        <span class="mono text-muted">{{ ch.name }}</span>
        <el-switch
          :model-value="isOn(ch.name, ch.bool)"
          size="small"
          @change="(v: string | number | boolean) => setOn(ch.name, !!v, ch.bool)"
        />
        <el-button v-if="ch.keys.length" size="small" text :loading="testing === ch.name" @click="test(ch.name)">
          测试
        </el-button>
      </div>
      <div class="chan__keys">
        <div v-for="k in ch.keys" :key="k" class="kv">
          <label class="field-label mono">{{ k }}</label>
          <el-input
            :model-value="getVal(k)"
            placeholder="（未配置）"
            @update:model-value="(v: string) => setVal(k, v)"
          />
        </div>
        <div v-if="!ch.keys.length" class="text-muted bool-hint">
          布尔开关：{{ isOn(ch.name, ch.bool) ? "开" : "关" }}
        </div>
      </div>
      <div v-if="results[ch.name]" class="res">
        <span v-for="(r, i) in results[ch.name]" :key="i" class="res__item" :class="r.ok ? 'ok' : 'bad'"
          >{{ r.ok ? "✓" : "✗" }} {{ r.message }}</span
        >
      </div>
    </div>

    <div v-if="results['__all']" class="allres card">
      <b>全部测试结果</b>
      <div v-for="(r, i) in results['__all']" :key="i" class="res__item" :class="r.ok ? 'ok' : 'bad'">
        [{{ r.channel }}] {{ r.message }}
      </div>
    </div>
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
.chan {
  padding: 12px 14px;
}
.chan__top {
  display: flex;
  align-items: center;
  gap: 10px;
}
.chan__name {
  font-weight: 600;
}
.chan__keys {
  margin-top: 10px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 10px;
}
.bool-hint {
  font-size: 13px;
}
.res {
  margin-top: 8px;
}
.res__item {
  font-size: 12.5px;
  display: block;
}
.res__item.ok {
  color: #1c9e6e;
}
.res__item.bad {
  color: var(--danger);
}
.allres {
  padding: 12px 14px;
}
</style>
