<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api, type ApiTokenInfo, type MigratePreview } from "../api/client";

const tokens = ref<ApiTokenInfo[]>([]);
const newToken = ref("");
const tokenName = ref("油猴脚本");
const busy = ref(false);

// 迁移
const raw = ref("");
const preview = ref<MigratePreview | null>(null);
const overwrite = ref(false);
const migrating = ref(false);

async function load() {
  try {
    tokens.value = await api.listTokens();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}
onMounted(load);

async function create() {
  busy.value = true;
  try {
    const r = await api.createToken(tokenName.value.trim() || "未命名");
    newToken.value = r.token;
    await load();
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    busy.value = false;
  }
}

function copyToken() {
  navigator.clipboard?.writeText(newToken.value).then(
    () => ElMessage.success("已复制，完整 Token 只显示这一次，请妥善保存"),
    () => ElMessage.warning("复制失败，请手动选中复制"),
  );
}

async function remove(t: ApiTokenInfo) {
  await ElMessageBox.confirm(`删除 Token「${t.name}」？使用该 Token 的脚本将立即失效。`, "确认", { type: "warning" });
  const full = prompt("请输入该 Token 的完整值以确认删除（列表仅显示掩码）");
  if (!full) return;
  try {
    await api.deleteToken(full);
    await load();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

async function doPreview() {
  preview.value = null;
  try {
    preview.value = await api.migratePreview(JSON.parse(raw.value));
  } catch (e) {
    ElMessage.error("JSON 解析失败：" + (e as Error).message.slice(0, 80));
    preview.value = null;
  }
}

async function doImport() {
  if (!preview.value) return void (await doPreview());
  migrating.value = true;
  try {
    const r = await api.migrateImport(JSON.parse(raw.value), overwrite.value);
    ElMessage.success(`导入完成：账号 ${r.imported_accounts} 个、任务 ${r.imported_tasks} 个`);
    raw.value = "";
    preview.value = null;
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    migrating.value = false;
  }
}
</script>

<template>
  <div class="pane">
    <h3>API 与迁移</h3>
    <p class="text-muted">
      对外接口：<code>POST /api/add_task?token=…</code
      >（兼容旧油猴格式）、<code>/api/v1/task/add|list|update|run</code>。 配套脚本：<code
        >scripts/xiao-pan-auto-save.user.js</code
      >
    </p>

    <h4>API Token</h4>
    <div class="row">
      <el-input v-model="tokenName" placeholder="备注名，如：油猴脚本" style="width: 200px" />
      <el-button type="primary" :loading="busy" @click="create">生成 Token</el-button>
    </div>
    <el-alert v-if="newToken" type="success" :closable="false" class="tok">
      <div class="tok__line">
        <code class="mono">{{ newToken }}</code>
        <el-button size="small" @click="copyToken">复制</el-button>
      </div>
      <div class="text-muted">完整值仅显示这一次，关闭后只能删除重建。</div>
    </el-alert>
    <table v-if="tokens.length" class="tbl">
      <thead>
        <tr>
          <th>备注</th>
          <th>Token</th>
          <th>创建时间</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="t in tokens" :key="t.token_preview">
          <td>{{ t.name }}</td>
          <td class="mono">{{ t.token_preview }}</td>
          <td>{{ t.created_at.replace("T", " ").slice(0, 16) }}</td>
          <td>
            <el-button size="small" text type="danger" @click="remove(t)">删除</el-button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="text-muted">尚无 Token，未配置时对外 API 一律返回 401。</p>

    <el-divider />
    <h4>从 quark-auto-save 迁移</h4>
    <p class="text-muted">粘贴旧版 quark_config.json 全文，先预览再导入。</p>
    <el-input v-model="raw" type="textarea" :rows="6" placeholder='{ "cookie": "...", "tasklist": [ ... ] }' />
    <div class="row" style="margin-top: 8px">
      <el-button :disabled="!raw.trim()" @click="doPreview">预览</el-button>
      <el-checkbox v-model="overwrite">覆盖导入（清空现有任务与夸克账号）</el-checkbox>
      <el-button type="primary" :disabled="!preview" :loading="migrating" @click="doImport">确认导入</el-button>
    </div>
    <el-alert v-if="preview" type="info" :closable="false" class="tok">
      将导入：夸克账号 <b>{{ preview.accounts }}</b> 个 · 任务 <b>{{ preview.tasks }}</b> 个 · 设置项
      {{ preview.settings.join("、") || "无" }}
      <template v-if="preview.plugin_tasks_ignored.length">
        <br />插件配置不支持迁移，以下任务的 addition 将被忽略：{{ preview.plugin_tasks_ignored.join("、") }}
      </template>
    </el-alert>
  </div>
</template>

<style scoped>
.pane h3 {
  margin: 0 0 6px;
}
.pane h4 {
  margin: 14px 0 8px;
  font-size: 14px;
}
.row {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.tok {
  margin-top: 10px;
}
.tok__line {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.tbl {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
  font-size: 13px;
}
.tbl th,
.tbl td {
  text-align: left;
  padding: 7px 8px;
  border-bottom: 1px solid var(--border);
}
.tbl th {
  color: var(--text-muted);
  font-weight: 500;
}
code {
  background: #f0f2f5;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 12px;
}
</style>
