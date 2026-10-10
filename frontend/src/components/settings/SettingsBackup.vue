<script setup lang="ts">
import { ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../../api/client";
import { formatDateTime } from "../../utils";

const busy = ref("");
const lastExport = ref("");
const lastImport = ref("");

function download(payload: unknown, mode: string) {
  const name = `xiao-pan-backup-${mode}-${new Date().toISOString().slice(0, 10)}.json`;
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
  return name;
}

async function exportBackup(mode: "safe" | "full") {
  if (mode === "full") {
    try {
      await ElMessageBox.confirm(
        "含凭据备份会把网盘 Cookie 明文写进文件。请只在自己保管的本机使用，不要外发或存到网盘。",
        "确认导出含凭据备份",
        { type: "warning", confirmButtonText: "我明白，继续导出", cancelButtonText: "取消" },
      );
    } catch {
      return;
    }
  }
  busy.value = mode;
  try {
    const r = await api.exportBackup(mode);
    const name = download(r.data, mode);
    lastExport.value = `${formatDateTime(new Date().toISOString())} 导出 ${name}（任务 ${r.data.meta.counts.tasks} / 账号 ${r.data.meta.counts.accounts} / 下载记录 ${r.data.meta.counts.downloads}）`;
    ElMessage.success("已导出");
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    busy.value = "";
  }
}

async function onFile(ev: Event) {
  const input = ev.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  try {
    const text = await file.text();
    const payload = JSON.parse(text);
    const hasCred = payload?.meta?.credentials_included === true;
    await ElMessageBox.confirm(
      hasCred
        ? "这份备份包含凭据，将覆盖当前全部任务、账号、设置与下载记录。恢复前会自动备份当前数据。"
        : "将覆盖当前全部任务、账号、设置与下载记录；账号 Cookie 需重新录入。恢复前会自动备份当前数据。",
      "确认恢复",
      { type: "warning", confirmButtonText: "确认恢复", cancelButtonText: "取消" },
    );
    busy.value = "import";
    const r = await api.importBackup(payload);
    lastImport.value = `${formatDateTime(new Date().toISOString())} 恢复完成：任务 ${r.restored?.tasks ?? 0} / 账号 ${r.restored?.accounts ?? 0} / 下载 ${r.restored?.downloads ?? 0}`;
    ElMessage.success("恢复完成，建议刷新页面");
  } catch (e) {
    if ((e as Error).message !== "cancel") ElMessage.error((e as Error).message);
  } finally {
    busy.value = "";
    input.value = "";
  }
}
</script>

<template>
  <div class="pane">
    <h3>备份与恢复</h3>
    <p class="hint">
      导出为结构化 JSON，包含任务、账号、设置与下载记录。默认导出<b>不含凭据</b>—— 整库快照会把 Cookie
      明文一起带走，外发或存网盘就成了凭据泄露面。
    </p>

    <div class="actions">
      <button class="btn btn--primary" :disabled="!!busy" @click="exportBackup('safe')">
        {{ busy === "safe" ? "导出中…" : "导出（不含凭据，推荐）" }}
      </button>
      <button class="btn" :disabled="!!busy" @click="exportBackup('full')">
        {{ busy === "full" ? "导出中…" : "导出（含凭据）" }}
      </button>
      <label class="btn btn--ghost" :class="{ disabled: !!busy }">
        {{ busy === "import" ? "恢复中…" : "从文件恢复" }}
        <input type="file" accept="application/json" hidden @change="onFile" />
      </label>
    </div>

    <p v-if="lastExport" class="result">{{ lastExport }}</p>
    <p v-if="lastImport" class="result">{{ lastImport }}</p>

    <p class="hint warn">
      恢复是不可逆操作：会覆盖当前全部数据，执行前会自动给当前库留一份快照（<code>data/backups</code>）。
      更高版本的备份会被拒绝，请先升级程序。
    </p>
  </div>
</template>

<style scoped>
.pane h3 {
  margin: 0 0 6px;
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
.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.btn {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 8px;
  padding: 7px 14px;
  font-size: 13px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
}
.btn--primary {
  background: var(--primary);
  border-color: var(--primary);
  color: #fff;
}
.btn--ghost {
  color: var(--text-muted);
}
.btn.disabled {
  opacity: 0.5;
  pointer-events: none;
}
.result {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--text-muted);
}
code {
  background: #f0f2f5;
  padding: 1px 5px;
  border-radius: 4px;
}
</style>
