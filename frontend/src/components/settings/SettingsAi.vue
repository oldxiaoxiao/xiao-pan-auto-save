<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../../api/client";
import { formatDateTime } from "../../utils";
import type { AiConfig, AiUsage } from "../../api/types";

const form = ref<AiConfig & { api_key?: string; clear_key?: boolean }>({
  enabled: false,
  provider: "openai",
  base_url: "",
  model: "",
  has_key: false,
  api_key_masked: "",
  temperature: 0.2,
  timeout_ms: 30000,
  mode: "copilot",
  monthly_token_limit: 0,
  tool_call: true,
  api_key: "",
});
const loading = ref(true);
const saving = ref(false);
const testing = ref(false);
const testResult = ref("");
const usage = ref<AiUsage | null>(null);

const PRESETS = [
  { label: "OpenAI", provider: "openai", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  { label: "DeepSeek", provider: "openai", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  {
    label: "通义千问（兼容模式）",
    provider: "openai",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: "qwen-plus",
  },
  { label: "智谱 GLM", provider: "openai", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" },
  { label: "Moonshot / Kimi", provider: "openai", base_url: "https://api.moonshot.cn/v1", model: "moonshot-v1-8k" },
  {
    label: "硅基流动",
    provider: "openai",
    base_url: "https://api.siliconflow.cn/v1",
    model: "Qwen/Qwen2.5-7B-Instruct",
  },
  { label: "Ollama（本机）", provider: "openai", base_url: "http://localhost:11434/v1", model: "qwen2.5:7b" },
  {
    label: "Anthropic",
    provider: "anthropic",
    base_url: "https://api.anthropic.com",
    model: "claude-3-5-sonnet-latest",
  },
];

function applyPreset(p: (typeof PRESETS)[number]) {
  form.value.provider = p.provider;
  form.value.base_url = p.base_url;
  form.value.model = p.model;
}

async function load() {
  loading.value = true;
  try {
    const r = await api.aiConfig();
    form.value = { ...form.value, ...r.data, api_key: "" };
    try {
      const u = await api.aiUsage();
      usage.value = u.data;
    } catch {
      usage.value = null;
    }
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const r = await api.aiSaveConfig({
      provider: form.value.provider,
      base_url: form.value.base_url,
      model: form.value.model,
      api_key: form.value.api_key || "",
      clear_key: false,
      temperature: Number(form.value.temperature),
      timeout_ms: Number(form.value.timeout_ms),
      mode: form.value.mode,
      monthly_token_limit: Number(form.value.monthly_token_limit || 0),
      enabled: form.value.enabled,
      tool_call: form.value.tool_call,
    });
    form.value = { ...form.value, ...r.data, api_key: "" };
    ElMessage.success("已保存");
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    saving.value = false;
  }
}

async function test() {
  testing.value = true;
  testResult.value = "";
  try {
    const r = await api.aiTest({
      provider: form.value.provider,
      base_url: form.value.base_url,
      model: form.value.model,
      api_key: form.value.api_key || "",
      temperature: Number(form.value.temperature),
      timeout_ms: Number(form.value.timeout_ms),
    });
    testResult.value = r.ok
      ? `连通成功（${r.message}）${r.tool_call ? "，支持工具调用" : "，不支持工具调用，将只做只读问答"}`
      : `失败：${r.message}`;
  } catch (e) {
    testResult.value = `失败：${(e as Error).message}`;
  } finally {
    testing.value = false;
  }
}

async function clearKey() {
  await api.aiSaveConfig({ ...form.value, api_key: "", clear_key: true } as never);
  await load();
  ElMessage.success("已清除已保存的 Key");
}

onMounted(load);
</script>

<template>
  <div v-loading="loading" class="pane">
    <h3>AI 助手</h3>
    <p class="hint">
      助手需要你自己的模型服务。API Key 只加密保存在本机，界面与接口永远只回显掩码。 未启用前不会发起任何外部请求。
    </p>

    <div class="row">
      <label>启用助手</label>
      <el-switch v-model="form.enabled" size="small" />
    </div>

    <div class="presets">
      <button v-for="p in PRESETS" :key="p.label" class="chip" @click="applyPreset(p)">{{ p.label }}</button>
    </div>

    <div class="grid">
      <div class="row">
        <label>协议</label>
        <select v-model="form.provider" class="input">
          <option value="openai">OpenAI 兼容（Chat Completions）</option>
          <option value="anthropic">Anthropic（Messages）</option>
        </select>
      </div>
      <div class="row">
        <label>服务地址 Base URL</label>
        <input v-model="form.base_url" class="input" placeholder="https://api.openai.com/v1" />
      </div>
      <div class="row">
        <label>模型名</label>
        <input v-model="form.model" class="input" placeholder="gpt-4o-mini" />
      </div>
      <div class="row">
        <label>API Key</label>
        <div class="keyrow">
          <input v-model="form.api_key" class="input" type="password" placeholder="留空表示保持原值" />
          <span v-if="form.has_key" class="masked">已保存：{{ form.api_key_masked }}</span>
          <button v-if="form.has_key" class="btn btn--ghost" @click="clearKey">清除</button>
        </div>
      </div>
      <div class="row">
        <label>默认模式</label>
        <select v-model="form.mode" class="input">
          <option value="copilot">计划 + 确认（可建任务、可运行）</option>
          <option value="chat">只读问答（不生成任何操作）</option>
        </select>
      </div>
      <div class="row">
        <label>月度 token 上限（万）</label>
        <input v-model.number="form.monthly_token_limit" class="input" type="number" min="0" />
      </div>
      <div class="row">
        <label>温度</label>
        <input v-model.number="form.temperature" class="input" type="number" step="0.1" min="0" max="2" />
      </div>
      <div class="row">
        <label>超时（毫秒）</label>
        <input v-model.number="form.timeout_ms" class="input" type="number" min="1000" step="1000" />
      </div>
    </div>

    <div class="actions">
      <button class="btn btn--primary" :disabled="saving" @click="save">{{ saving ? "保存中…" : "保存" }}</button>
      <button class="btn" :disabled="testing" @click="test">{{ testing ? "测试中…" : "测试连通性" }}</button>
      <span v-if="testResult" class="test">{{ testResult }}</span>
    </div>

    <div v-if="usage" class="usage">
      <h4>本月用量</h4>
      <p class="hint">
        统计自 {{ formatDateTime(usage.since) }}：调用 {{ usage.calls }} 次（失败 {{ usage.failed }}）， 输入
        {{ usage.prompt_tokens }} / 输出 {{ usage.completion_tokens }} tokens。
      </p>
    </div>

    <p class="hint warn">
      隐私提示：开启后，任务名、文件名等上下文会发送给你自己配置的模型服务。介意请用本机模型（如 Ollama），或保持关闭。
    </p>
  </div>
</template>

<style scoped>
.pane h3 {
  margin: 0 0 6px;
}
.pane h4 {
  margin: 16px 0 4px;
}
.hint {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.7;
  margin: 0 0 12px;
}
.hint.warn {
  margin-top: 14px;
  padding: 8px 10px;
  background: #fff8ec;
  border: 1px solid #f5e3c3;
  border-radius: 8px;
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.row label {
  width: 150px;
  font-size: 13px;
  color: var(--text-muted);
  flex-shrink: 0;
}
.input {
  flex: 1;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 13px;
  min-width: 0;
}
.grid {
  margin-top: 12px;
}
.presets {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 10px 0;
}
.chip {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 999px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
}
.chip:hover {
  border-color: var(--primary);
  color: var(--primary);
}
.keyrow {
  display: flex;
  flex: 1;
  gap: 8px;
  align-items: center;
}
.masked {
  font-size: 12px;
  color: var(--text-muted);
  white-space: nowrap;
}
.actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
  flex-wrap: wrap;
}
.btn {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 8px;
  padding: 7px 14px;
  font-size: 13px;
  cursor: pointer;
}
.btn--primary {
  background: var(--primary);
  border-color: var(--primary);
  color: #fff;
}
.btn--ghost {
  color: var(--text-muted);
}
.test {
  font-size: 12px;
  color: var(--text-muted);
}
.usage {
  margin-top: 6px;
}
@media (max-width: 767px) {
  .row {
    flex-direction: column;
    align-items: flex-start;
  }
  .row label {
    width: auto;
  }
  .input {
    width: 100%;
  }
}
</style>
