<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { api } from "../api/client";
import AiDirPicker from "../components/AiDirPicker.vue";
import { formatDateTime } from "../utils";
import type { AiAction, AiConfig, AiMessage, AiSession, AiSource, AgentEvent } from "../api/types";

const SOURCE_LABELS: Record<string, string> = {
  task: "任务",
  tasks: "任务列表",
  downloads: "下载记录",
  accounts: "账号",
  search: "公开搜索源",
  share: "分享内容",
  dir: "网盘目录",
  settings: "设置",
  logs: "运行日志",
};

function sourceLabel(type: string) {
  return SOURCE_LABELS[type] || type;
}

const FIELD_LABELS: Record<string, string> = {
  taskname: "任务名",
  shareurl: "分享链接",
  savepath: "保存目录",
  quality: "画质",
  pattern: "正则筛选",
  replace: "正则替换",
  auto_download: "下载到本地",
  episode_start: "起始集",
  episode_end: "结束集",
  run_mode: "执行方式",
  run_after_create: "创建后立即运行",
  task_id: "任务",
  record_id: "下载记录",
  disabled: "停用",
  keyword: "关键词",
  deep: "深度搜索",
  limit: "条数",
  path: "路径",
  driver: "网盘",
};

const RUN_MODE_OPTIONS = [
  { value: "follow", label: "定时追更" },
  { value: "manual", label: "仅手动" },
  { value: "once", label: "只这一次" },
];

function fieldLabel(key: string) {
  return FIELD_LABELS[key] || key;
}

interface ChatMsg {
  id?: number;
  role: "user" | "assistant";
  content: string;
  sources: AiSource[];
  actions: AiAction[];
  confidence?: string;
  streaming?: boolean;
  tool?: string;
  feedback?: string;
}

const router = useRouter();
const config = ref<AiConfig | null>(null);
const sessions = ref<AiSession[]>([]);
const sessionId = ref<number | null>(null);
const msgs = ref<ChatMsg[]>([]);
const input = ref("");
const busy = ref(false);
const loading = ref(true);
const edits = reactive<Record<string, Record<string, string | number | boolean>>>({});
const streamEl = ref<HTMLElement | null>(null);
const cardLogs = ref<Record<string, string[]>>({});
let logEs: EventSource | null = null;
const pickerOpen = ref(false);
const pickerFor = ref("");
const pickerPath = ref("/");
const deployDesktop = ref<boolean | null>(null);
const deployLabel = computed(() =>
  deployDesktop.value === null ? "" : deployDesktop.value ? "桌面客户端（本机）" : "服务器 / NAS（容器）",
);

const deployHint = computed(() =>
  deployDesktop.value
    ? "程序在你这台电脑上跑：下载落到本机；关掉客户端或电脑休眠就不会追更、不会下载"
    : "程序常驻在服务器或 NAS：下载落到那台机器的磁盘，不是你打开网页用的这台电脑",
);

function openPicker(action: AiAction) {
  pickerFor.value = action.id;
  pickerPath.value = String(edits[action.id]?.savepath || "/");
  pickerOpen.value = true;
}

function onPickDir(path: string) {
  const id = pickerFor.value;
  if (id && edits[id]) edits[id].savepath = path;
  pickerOpen.value = false;
}

const ready = computed(() => !!config.value?.enabled && !!config.value?.has_key && !!config.value?.model);
const hasRunning = computed(() => msgs.value.some((m) => m.actions.some((a) => a.status === "running")));

/** 运行中的动作卡：订阅全局日志流，只取本任务的行，避免"点完只能去日志页看"。 */
function openLogTail() {
  if (logEs) return;
  logEs = new EventSource(api.logsStreamUrl());
  logEs.onmessage = (ev) => {
    let entry: { task_id?: number | null; message?: string; level?: string };
    try {
      entry = JSON.parse(ev.data);
    } catch {
      return;
    }
    // 运行结束的 done 行不带 task_id（是 run 级别），所以先单独处理收口刷新
    if (entry.level === "done" || entry.level === "summary") {
      setTimeout(() => void loadMessages(), 1500);
    }
    const tid = entry.task_id;
    if (!tid || !entry.message) return;
    for (const m of msgs.value) {
      for (const a of m.actions) {
        if (a.status !== "running" || Number(a.params.task_id) !== Number(tid)) continue;
        const arr = cardLogs.value[a.id] || (cardLogs.value[a.id] = []);
        arr.push(entry.message);
        if (arr.length > 60) arr.shift();
      }
    }
  };
}

function closeLogTail() {
  if (logEs) {
    logEs.close();
    logEs = null;
  }
}

watch(hasRunning, (v) => (v ? openLogTail() : closeLogTail()));

marked.setOptions({ gfm: true, breaks: true });

// 模型输出不可信，渲染前必须消毒；外链一律新窗口且切断 opener
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && node.getAttribute("href")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

function renderMd(text: string): string {
  try {
    return DOMPurify.sanitize(marked.parse(text || "", { async: false }) as string);
  } catch {
    return "";
  }
}

function riskLabel(risk: string) {
  return risk === "high" ? "高风险" : risk === "medium" ? "需留意" : "低风险";
}

function initEdits(msg: ChatMsg) {
  for (const a of msg.actions) {
    edits[a.id] = { ...a.params };
  }
}

async function scrollDown() {
  await nextTick();
  const el = streamEl.value;
  if (el) el.scrollTop = el.scrollHeight;
}

async function loadSessions() {
  const r = await api.aiSessions();
  sessions.value = r.data || [];
  if (!sessionId.value && sessions.value.length) {
    sessionId.value = sessions.value[0].id;
    await loadMessages();
  }
}

async function loadMessages() {
  if (!sessionId.value) return;
  const r = await api.aiMessages(sessionId.value);
  msgs.value = (r.data || []).map((m: AiMessage) => ({
    id: m.id,
    role: m.role,
    content: m.content,
    sources: m.sources || [],
    actions: m.actions || [],
    confidence: m.confidence,
    feedback: m.feedback,
  }));
  for (const m of msgs.value) initEdits(m);
  await scrollDown();
}

async function newSession() {
  const r = await api.aiCreateSession("新对话");
  await loadSessions();
  sessionId.value = r.data.id;
  msgs.value = [];
}

async function switchSession(id: number) {
  sessionId.value = id;
  await loadMessages();
}

async function removeSession() {
  if (!sessionId.value) return;
  await api.aiDeleteSession(sessionId.value);
  sessionId.value = null;
  msgs.value = [];
  await loadSessions();
}

function pushError(text: string) {
  msgs.value.push({ role: "assistant", content: text, sources: [], actions: [] });
}

async function send() {
  const text = input.value.trim();
  if (!text || busy.value || !sessionId.value) return;
  input.value = "";
  msgs.value.push({ role: "user", content: text, sources: [], actions: [] });
  const bot: ChatMsg = { role: "assistant", content: "", sources: [], actions: [], streaming: true };
  msgs.value.push(bot);
  busy.value = true;
  await scrollDown();
  try {
    await api.aiSend(sessionId.value, text, (e: AgentEvent) => {
      if (e.type === "delta") {
        bot.content += e.text || "";
      } else if (e.type === "tool") {
        bot.tool = e.status === "pending" ? `${e.name}：待确认` : `${e.name}：${e.status}`;
      } else if (e.type === "done") {
        bot.streaming = false;
        bot.id = e.message_id;
        bot.sources = e.sources || [];
        bot.actions = e.actions || [];
        bot.confidence = e.confidence;
        initEdits(bot);
      } else if (e.type === "error") {
        bot.streaming = false;
        pushError(e.message || "助手执行失败");
      }
      void scrollDown();
    });
  } catch (e) {
    bot.streaming = false;
    pushError((e as Error).message || "无法连接到后端服务");
  } finally {
    busy.value = false;
    bot.tool = "";
    await scrollDown();
  }
}

async function runAction(_msg: ChatMsg, action: AiAction) {
  const params = edits[action.id] || action.params;
  try {
    const r = await api.aiExecuteAction(action.id, params);
    if (r.started) {
      action.status = "running";
      action.result = r.message;
      ElMessage.info(r.message || "已开始运行");
      // 运行是异步的：几秒后回拉一次会话，把最终状态带回来
      setTimeout(() => void loadMessages(), 5000);
      return;
    }
    action.status = r.ok ? "done" : "failed";
    action.result = r.message;
    action.edited = true;
    ElMessage.success(r.message || "已执行");
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

async function feedback(msg: ChatMsg, value: string) {
  if (!msg.id) return;
  msg.feedback = msg.feedback === value ? "" : value;
  try {
    await api.aiFeedback(msg.id, msg.feedback);
  } catch {
    /* 反馈失败不影响会话 */
  }
}

function goto(ref_: string) {
  if (ref_.startsWith("/")) void router.push(ref_);
}

onMounted(async () => {
  try {
    const h = await api.health();
    deployDesktop.value = h.desktop_mode === true;
  } catch {
    deployDesktop.value = null; // 拿不到就不猜，不显示部署形态
  }
  try {
    const cfg = await api.aiConfig();
    config.value = cfg.data;
    if (cfg.data?.enabled) await loadSessions();
  } catch (e) {
    pushError((e as Error).message);
  } finally {
    loading.value = false;
  }
});

onUnmounted(closeLogTail);
</script>

<template>
  <div v-loading="loading" class="chat">
    <div class="sticky-bar">
      <span class="sticky-bar__title">AI 助手</span>
      <div class="bar__right">
        <span v-if="deployLabel" class="deploy" :title="deployHint">{{ deployLabel }}</span>
        <select v-model="sessionId" class="sel" @change="switchSession(Number(sessionId))">
          <option v-for="s in sessions" :key="s.id" :value="s.id">
            {{ s.title }} · {{ formatDateTime(s.last_at) }}
          </option>
        </select>
        <button class="btn" @click="newSession">新建对话</button>
        <button class="btn btn--ghost" @click="removeSession">删除本对话</button>
      </div>
    </div>

    <div v-if="!ready" class="card setup">
      <h3>还没有配置 AI 服务</h3>
      <p>
        助手需要你自己的模型服务才能工作：填入服务地址、模型名与 API Key（Key 只加密保存在本机）。
        未配置前不会发起任何外部请求。
      </p>
      <RouterLink class="btn btn--primary" to="/settings?g=ai">去设置 → AI 助手</RouterLink>
    </div>

    <div v-else class="card body">
      <div ref="streamEl" class="stream">
        <div v-if="!msgs.length" class="empty-state">
          试试问我：「有哪些任务？」「XX 最近一次运行是什么时候？」「帮我找 XX 的资源」
        </div>

        <div v-for="(m, i) in msgs" :key="i" class="msg" :class="'msg--' + m.role">
          <div class="bubble">
            <div class="content md" :class="{ streaming: m.streaming }" v-html="renderMd(m.content)" />

            <div v-if="m.tool" class="tool">正在调用 {{ m.tool }}</div>

            <div v-if="m.sources?.length" class="sources">
              <span class="src-label">数据来源</span>
              <button v-for="(s, k) in m.sources" :key="k" class="src" @click="goto(s.ref)">
                {{ sourceLabel(s.type) }} · {{ formatDateTime(s.as_of) }}
              </button>
            </div>

            <div v-for="a in m.actions" :key="a.id" class="action">
              <div class="action__head">
                <span class="risk" :class="'risk--' + a.risk">{{ riskLabel(a.risk) }}</span>
                <span class="action__summary">{{ a.summary }}</span>
              </div>
              <div class="action__impact">{{ a.impact }}（需你确认后才会执行）</div>

              <div v-if="a.status === 'pending'" class="action__params">
                <label v-for="(v, k) in a.params" :key="String(k)" class="param">
                  <span class="param__key">{{ fieldLabel(String(k)) }}</span>
                  <template v-if="String(k) === 'run_mode'">
                    <select v-model="edits[a.id][String(k)]" class="input">
                      <option v-for="o in RUN_MODE_OPTIONS" :key="o.value" :value="o.value">
                        {{ o.label }}
                      </option>
                    </select>
                  </template>
                  <template v-else-if="typeof v === 'boolean'">
                    <input v-model="edits[a.id][String(k)]" type="checkbox" class="param__check" />
                    <span class="param__bool">{{ edits[a.id][String(k)] ? "开启" : "关闭" }}</span>
                  </template>
                  <template v-else>
                    <input
                      v-model="edits[a.id][String(k)]"
                      class="input"
                      :title="String(edits[a.id][String(k)] ?? '')"
                    />
                    <button v-if="String(k) === 'savepath'" class="mini" type="button" @click.prevent="openPicker(a)">
                      浏览
                    </button>
                  </template>
                </label>
              </div>

              <div class="action__foot">
                <template v-if="a.status === 'pending'">
                  <button class="btn btn--primary" @click="runAction(m, a)">确认执行</button>
                  <button
                    class="btn btn--ghost"
                    @click="
                      a.status = 'failed';
                      a.result = '用户取消';
                    "
                  >
                    取消
                  </button>
                </template>
                <span v-else-if="a.status === 'running'" class="action__result running"> 运行中… {{ a.result }} </span>
                <div v-if="cardLogs[a.id]?.length" class="tailing">
                  <div v-for="(line, k) in cardLogs[a.id].slice(-6)" :key="k" class="tailing__line">
                    {{ line }}
                  </div>
                </div>
                <span v-else class="action__result">
                  {{ a.status === "done" ? "已执行" : "未执行" }}：{{ a.result }}
                </span>
              </div>
            </div>

            <div v-if="m.role === 'assistant' && !m.streaming && m.content" class="fb">
              <span class="fb__label">这个回答</span>
              <button class="fb__btn" :class="{ on: m.feedback === 'up' }" @click="feedback(m, 'up')">
                <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8">
                  <path d="M7 21V10l4-7a2 2 0 0 1 3.6 1.5L13.5 9H19a2 2 0 0 1 2 2.4l-1.4 7A2 2 0 0 1 17.6 20H7z" />
                  <path d="M3 10h4v11H3z" />
                </svg>
                有用
              </button>
              <button class="fb__btn" :class="{ on: m.feedback === 'down' }" @click="feedback(m, 'down')">
                <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8">
                  <path d="M17 3v11l-4 7a2 2 0 0 1-3.6-1.5L10.5 15H5a2 2 0 0 1-2-2.4l1.4-7A2 2 0 0 1 6.4 4H17z" />
                  <path d="M21 14h-4V3h4z" />
                </svg>
                无用
              </button>
              <span v-if="m.confidence === 'low'" class="fb__hint">置信度低，建议按来源自行核对</span>
            </div>
          </div>
        </div>
      </div>

      <div class="composer">
        <textarea
          v-model="input"
          class="ta"
          rows="2"
          placeholder="用自然语言问我或下达指令（Enter 发送，Shift+Enter 换行）"
          @keydown.enter.exact.prevent="send"
        />
        <button class="btn btn--primary" :disabled="busy || !input.trim()" @click="send">
          {{ busy ? "处理中…" : "发送" }}
        </button>
      </div>
    </div>

    <AiDirPicker v-model="pickerOpen" :path="pickerPath" @pick="onPickDir" />
  </div>
</template>

<style scoped>
.sticky-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.sticky-bar__title {
  font-size: 18px;
  font-weight: 600;
}
.bar__right {
  display: flex;
  gap: 8px;
  align-items: center;
}
.deploy {
  font-size: 11px;
  color: var(--text-muted);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 2px 8px;
  cursor: help;
}
.mini {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 6px;
  padding: 3px 8px;
  font-size: 11px;
  color: var(--primary);
  cursor: pointer;
  flex-shrink: 0;
}
.mini:hover {
  border-color: var(--primary);
}
.sel {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 8px;
  font-size: 13px;
  background: #fff;
  max-width: 180px;
}
.body {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 140px);
  padding: 0;
  overflow: hidden;
}
.stream {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}
.empty-state {
  text-align: center;
  color: var(--text-muted);
  padding: 40px 10px;
  font-size: 13px;
}
.msg {
  display: flex;
  margin-bottom: 14px;
}
.msg--user {
  justify-content: flex-end;
}
.bubble {
  max-width: 78%;
  background: #f4f6fa;
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg--user .bubble {
  background: var(--primary-soft);
}
/* 流式光标用 ::after，避免被 v-html 覆盖 */
.content.md.streaming::after {
  content: "";
  display: inline-block;
  width: 6px;
  height: 14px;
  background: var(--primary);
  margin-left: 3px;
  vertical-align: -2px;
  animation: blink 1s steps(2) infinite;
}
@keyframes blink {
  50% {
    opacity: 0.2;
  }
}
.tool {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-muted);
}
/* v-html 的内容不受 scoped 限制，必须 :deep */
.content.md {
  white-space: normal;
}
.md :deep(p) {
  margin: 0 0 8px;
}
.md :deep(p:last-child) {
  margin-bottom: 0;
}
.md :deep(h1),
.md :deep(h2),
.md :deep(h3),
.md :deep(h4) {
  margin: 12px 0 6px;
  font-size: 14px;
  font-weight: 600;
}
.md :deep(h1):first-child,
.md :deep(h2):first-child,
.md :deep(h3):first-child {
  margin-top: 0;
}
.md :deep(ul),
.md :deep(ol) {
  margin: 6px 0;
  padding-left: 22px;
}
.md :deep(li) {
  margin: 3px 0;
}
.md :deep(strong) {
  font-weight: 600;
}
.md :deep(code) {
  background: rgba(0, 0, 0, 0.06);
  padding: 1px 5px;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.md :deep(pre) {
  background: #0f1420;
  color: #c9d3e4;
  padding: 10px 12px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 8px 0;
}
.md :deep(pre code) {
  background: none;
  color: inherit;
  padding: 0;
}
.md :deep(blockquote) {
  margin: 8px 0;
  padding: 6px 10px;
  border-left: 3px solid var(--border);
  color: var(--text-muted);
}
.md :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
  font-size: 12px;
  min-width: 100%;
}
.md :deep(th),
.md :deep(td) {
  border: 1px solid var(--border);
  padding: 5px 8px;
  text-align: left;
}
.md :deep(th) {
  background: #f7f8fa;
  font-weight: 600;
}
.md :deep(a) {
  color: var(--primary);
}
.md :deep(hr) {
  border: none;
  border-top: 1px solid var(--border);
  margin: 10px 0;
}
.sources {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.src-label {
  font-size: 12px;
  color: var(--text-muted);
}
.src {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 11px;
  cursor: pointer;
  color: var(--primary);
}
.action {
  margin-top: 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px;
  background: #fff;
}
.action__head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.action__summary {
  font-weight: 600;
}
.risk {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
}
.risk--high {
  background: #fdeceb;
  color: var(--danger);
}
.risk--medium {
  background: #fff5e6;
  color: var(--warn);
}
.risk--low {
  background: #e8f8f1;
  color: var(--success);
}
.action__impact {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-muted);
}
.action__params {
  margin-top: 10px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 8px 14px;
}
.param {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  min-width: 0;
}
.param__key {
  width: 92px;
  flex-shrink: 0;
  text-align: right;
  color: var(--text-muted);
}
.param__check {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}
.param__bool {
  color: var(--text-muted);
}
.input {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 12px;
}
.input:focus {
  outline: none;
  border-color: var(--primary);
}
.action__foot {
  margin-top: 10px;
  display: flex;
  gap: 8px;
  align-items: center;
}
.action__result {
  font-size: 12px;
  color: var(--text-muted);
}
.action__result.running {
  color: var(--primary);
}
.tailing {
  margin-top: 8px;
  background: #0f1420;
  border-radius: 6px;
  padding: 8px 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  line-height: 1.6;
  color: #c9d3e4;
  max-height: 120px;
  overflow-y: auto;
}
.tailing__line {
  white-space: pre-wrap;
  word-break: break-all;
}
.fb {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.fb__label {
  font-size: 12px;
  color: var(--text-muted);
}
.fb__btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 999px;
  padding: 4px 10px;
  font-size: 12px;
  color: var(--text-muted);
  cursor: pointer;
  transition: all 0.15s;
}
.fb__btn:hover {
  border-color: var(--primary);
  color: var(--primary);
}
.fb__btn.on {
  background: var(--primary-soft);
  border-color: var(--primary);
  color: var(--primary);
  font-weight: 600;
}
.fb__hint {
  font-size: 11px;
  color: var(--warn);
}
.composer {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid var(--border);
}
.ta {
  flex: 1;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 13px;
  font-family: inherit;
  resize: none;
}
.setup {
  padding: 24px;
}
.setup h3 {
  margin: 0 0 8px;
}
.setup p {
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.7;
}
.btn {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 8px;
  padding: 6px 12px;
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
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
@media (max-width: 767px) {
  .bubble {
    max-width: 100%;
  }
  .bar__right {
    flex-wrap: wrap;
  }
  .action__params {
    grid-template-columns: 1fr;
  }
  .param__key {
    width: 84px;
  }
}
</style>
