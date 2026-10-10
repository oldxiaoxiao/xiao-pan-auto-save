<script setup lang="ts">
import { ref, computed, onMounted, reactive } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { storeToRefs } from "pinia";
import { useAccountsStore } from "../stores/accounts";
import { api } from "../api/client";
import type { Account, AccountPayload, DriverInfo } from "../api/types";
import { formatGB, relativeTime } from "../utils";

const store = useAccountsStore();
const { sorted, loading, error } = storeToRefs(store);

const drivers = ref<DriverInfo[]>([]);
const dialog = ref(false);
const editing = ref<Account | null>(null);
const form = reactive<AccountPayload>({
  name: "",
  driver_key: "quark",
  cookie: "",
  enabled: true,
  sort_order: 0,
});

const driverName = computed(() => new Map(drivers.value.map((d) => [d.key, d.name])));

/** 失效 = 检查过且结论为不可用。没检查过的账号 check_ok 是默认值，不能误标。 */
function isInvalid(a: Account) {
  return a.check_ok === false && !!a.last_check_at;
}

function openAdd() {
  editing.value = null;
  Object.assign(form, { name: "", driver_key: "quark", cookie: "", enabled: true, sort_order: sorted.value.length });
  dialog.value = true;
}

function openEdit(acc: Account) {
  editing.value = acc;
  Object.assign(form, {
    name: acc.name,
    driver_key: acc.driver_key,
    cookie: "",
    enabled: acc.enabled,
    sort_order: acc.sort_order,
  });
  dialog.value = true;
}

async function submit() {
  if (!form.cookie.trim()) return ElMessage.warning("请粘贴 Cookie");
  try {
    if (editing.value) {
      await store.saveAccount(editing.value.id, { ...form });
      ElMessage.success("已更新账号");
    } else {
      await store.createAccount({ ...form });
      ElMessage.success("已添加账号");
    }
    dialog.value = false;
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

async function toggleEnabled(acc: Account) {
  try {
    if (!store.hasCookie(acc.id)) return ElMessage.warning("请编辑该账号重新粘贴 Cookie 后再切换启用");
    await store.patchMeta(acc.id, { enabled: !acc.enabled });
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

async function remove(acc: Account) {
  try {
    await ElMessageBox.confirm(`删除账号「${acc.nickname || acc.name || acc.id}」？`, "删除确认", { type: "warning" });
    await store.deleteAccount(acc.id);
    ElMessage.success("已删除");
  } catch (e) {
    if (e !== "cancel" && e instanceof Error) ElMessage.error(e.message);
  }
}

async function doRefresh() {
  try {
    const r = await store.refreshAll();
    ElMessage.success(`检查完成：${r.filter((x) => x.ok).length}/${r.length} 有效`);
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

async function doSign() {
  try {
    const r = await store.signAll();
    const rewards = r.filter((x) => x.ok).map((x) => x.nickname || x.id);
    ElMessage.success(`签到完成：${rewards.length} 个成功`);
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

function capPercent(a: Account): number {
  if (!a.capacity_total) return 0;
  return Math.min(100, Math.round((a.capacity_used / a.capacity_total) * 100));
}

// —— 拖拽排序 ——
const dragId = ref<number | null>(null);
function onDragStart(id: number) {
  dragId.value = id;
}
function onDragOver(ev: DragEvent, id: number) {
  if (dragId.value === null || dragId.value === id) return;
  ev.preventDefault();
  const order = sorted.value.map((a) => a.id);
  const from = order.indexOf(dragId.value);
  const to = order.indexOf(id);
  order.splice(to, 0, order.splice(from, 1)[0]);
  store.accounts = order.map((aid, i) => {
    const a = store.accounts.find((x) => x.id === aid)!;
    return { ...a, sort_order: i };
  });
}
async function onDragEnd() {
  if (dragId.value === null) return;
  dragId.value = null;
  try {
    await store.reorder(sorted.value.map((a) => a.id));
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

onMounted(async () => {
  store.fetchAccounts();
  try {
    drivers.value = await api.drivers();
  } catch {
    /* 驱动列表失败不阻塞账号页 */
  }
});
</script>

<template>
  <div>
    <div class="sticky-bar">
      <span class="sticky-bar__title">网盘账号</span>
      <el-button @click="doRefresh"> 全部检查 </el-button>
      <el-button @click="doSign"> 全部签到 </el-button>
      <el-button type="primary" @click="openAdd"> ＋ 添加账号 </el-button>
    </div>

    <p v-if="error" class="err">
      {{ error }}
    </p>
    <div v-else-if="!sorted.length && !loading" class="empty-state card">
      <span class="emoji">🪪</span>
      还没有网盘账号，添加账号后即可自动转存与签到
    </div>

    <div v-else v-loading="loading" class="grid">
      <div
        v-for="a in sorted"
        :key="a.id"
        class="acc card"
        :class="{ 'acc--invalid': isInvalid(a) }"
        draggable="true"
        @dragstart="onDragStart(a.id)"
        @dragover="onDragOver($event, a.id)"
        @dragend="onDragEnd"
      >
        <div class="acc__top">
          <span class="handle">⠿</span>
          <span class="nick">{{ a.nickname || a.name || `账号 #${a.id}` }}</span>
          <span class="badge badge--primary">{{ driverName.get(a.driver_key) || a.driver_key }}</span>
          <span v-if="isInvalid(a)" class="badge badge--danger" :title="a.check_message"> 需更新 </span>
          <el-switch :model-value="a.enabled" size="small" class="sw" @change="toggleEnabled(a)" />
        </div>

        <div v-if="isInvalid(a)" class="invalid">
          {{ a.check_message || "账号不可用" }}
          <template v-if="a.enabled"> — 该账号的任务已暂停执行，重新粘贴 Cookie 后自动恢复 </template>
        </div>

        <div v-if="a.capacity_total" class="cap">
          <div class="cap__bar">
            <div class="cap__fill" :style="{ width: capPercent(a) + '%' }" />
          </div>
          <div class="cap__text text-muted">
            {{ formatGB(a.capacity_used) }} / {{ formatGB(a.capacity_total) }} GB · {{ capPercent(a) }}%
          </div>
        </div>

        <div class="meta text-muted">
          <span v-if="a.member_type" class="chip">{{ a.member_type }}</span>
          <span v-if="a.can_save" class="chip is-primary">可转存</span>
          <span class="chip mono">CK {{ a.cookie_masked }}</span>
        </div>

        <div class="times text-muted">
          <div v-if="a.last_sign_at">
            签到 {{ relativeTime(a.last_sign_at) }}{{ a.sign_message ? "：" + a.sign_message : "" }}
          </div>
          <div v-if="a.last_check_at">检查 {{ relativeTime(a.last_check_at) }}</div>
        </div>

        <div class="acc__foot">
          <el-button size="small" text @click="openEdit(a)"> 编辑 </el-button>
          <el-button size="small" text type="danger" @click="remove(a)"> 删除 </el-button>
        </div>
      </div>
    </div>

    <el-dialog v-model="dialog" :title="editing ? '编辑账号' : '添加账号'" width="min(520px, 92vw)">
      <el-form label-width="84px" label-position="top">
        <el-form-item label="驱动">
          <el-select v-model="form.driver_key" style="width: 100%">
            <el-option
              v-for="d in drivers"
              :key="d.key"
              :label="d.supported ? d.name : `${d.name}（即将支持）`"
              :value="d.key"
              :disabled="!d.supported"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="备注名">
          <el-input v-model="form.name" placeholder="留空则用昵称" />
        </el-form-item>
        <el-form-item :label="editing ? 'Cookie（重新粘贴以保存）' : 'Cookie'">
          <el-input v-model="form.cookie" type="textarea" :rows="4" placeholder="粘贴完整网盘 Cookie" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false"> 取消 </el-button>
        <el-button type="primary" @click="submit"> 保存 </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}
.acc {
  display: flex;
  flex-direction: column;
  gap: 10px;
  cursor: grab;
}
.acc__top {
  display: flex;
  align-items: center;
  gap: 8px;
}
.handle {
  color: var(--text-muted);
  cursor: grab;
}
.nick {
  font-weight: 600;
  font-size: 15px;
  margin-right: auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sw {
  margin-left: auto;
}
.cap__bar {
  height: 8px;
  background: #eef0f3;
  border-radius: 6px;
  overflow: hidden;
}
.cap__fill {
  height: 100%;
  background: var(--primary);
}
.cap__text {
  font-size: 12px;
  margin-top: 4px;
}
.meta {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.times {
  font-size: 12px;
  line-height: 1.7;
}
.acc__foot {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}
.err {
  color: var(--danger);
}
.badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  white-space: nowrap;
}
.badge--primary {
  background: var(--primary-soft);
  color: var(--primary);
}
.badge--danger {
  background: #fdeceb;
  color: var(--danger);
  font-weight: 600;
}
.acc--invalid {
  border: 1px solid #f7cbc9;
}
.invalid {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--danger);
  background: #fdf3f2;
  border-radius: 6px;
  padding: 6px 8px;
}
</style>
