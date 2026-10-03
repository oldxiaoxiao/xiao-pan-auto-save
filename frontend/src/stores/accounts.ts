import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { api } from "../api/client";
import type { Account, AccountActionResult, AccountPayload } from "../api/types";

/**
 * 账号 store。
 *
 * 注意后端约束：PUT /api/accounts/{id} 需要完整 AccountPayload（含非空 cookie），
 * 而 GET 只返回 cookie_masked。因此启用开关与拖拽排序需要最近一次已知的明文 Cookie，
 * 用内存 cache 维护；页面刷新后 cache 丢失，需重新在编辑弹窗粘贴 Cookie 才能保存。
 */
export const useAccountsStore = defineStore("accounts", () => {
  const accounts = ref<Account[]>([]);
  const loading = ref(false);
  const error = ref("");
  const cookieCache = new Map<number, string>();

  const sorted = computed(() => [...accounts.value].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id));

  async function fetchAccounts() {
    loading.value = true;
    error.value = "";
    try {
      accounts.value = await api.listAccounts();
    } catch (e) {
      error.value = (e as Error).message;
    } finally {
      loading.value = false;
    }
  }

  function replace(next: Account) {
    const idx = accounts.value.findIndex((a) => a.id === next.id);
    if (idx >= 0) accounts.value[idx] = next;
    else accounts.value.push(next);
    accounts.value = [...accounts.value];
  }

  function rememberCookie(id: number, cookie: string) {
    cookieCache.set(id, cookie);
  }

  function hasCookie(id: number): boolean {
    return cookieCache.has(id);
  }

  async function createAccount(payload: AccountPayload): Promise<Account> {
    const created = await api.createAccount(payload);
    rememberCookie(created.id, payload.cookie);
    replace(created);
    return created;
  }

  /** 完整更新（弹窗提交，携带 cookie）。 */
  async function saveAccount(id: number, payload: AccountPayload): Promise<Account> {
    const updated = await api.updateAccount(id, payload);
    rememberCookie(updated.id, payload.cookie);
    replace(updated);
    return updated;
  }

  /** 仅改启用/排序：使用缓存 cookie 组装 PUT body；无缓存时抛错提示重新粘贴。 */
  async function patchMeta(id: number, patch: { enabled?: boolean; sort_order?: number }): Promise<Account> {
    const acc = accounts.value.find((a) => a.id === id);
    if (!acc) throw new Error("账号不存在");
    const cookie = cookieCache.get(id);
    if (!cookie) throw new Error("缺少 Cookie，请编辑该账号重新粘贴后再保存");
    const payload: AccountPayload = {
      name: acc.name,
      driver_key: acc.driver_key,
      cookie,
      enabled: patch.enabled ?? acc.enabled,
      sort_order: patch.sort_order ?? acc.sort_order,
    };
    const updated = await api.updateAccount(id, payload);
    replace(updated);
    return updated;
  }

  async function deleteAccount(id: number) {
    await api.deleteAccount(id);
    accounts.value = accounts.value.filter((a) => a.id !== id);
    cookieCache.delete(id);
  }

  async function reorder(orderedIds: number[]) {
    const byId = new Map(accounts.value.map((a) => [a.id, a]));
    const changes: number[] = [];
    orderedIds.forEach((id, i) => {
      const a = byId.get(id);
      if (a && a.sort_order !== i) changes.push(id);
    });
    for (const [i, id] of orderedIds.entries()) {
      const a = byId.get(id);
      if (a && a.sort_order !== i) await patchMeta(id, { sort_order: i });
    }
  }

  async function refreshAll(): Promise<AccountActionResult[]> {
    const results = await api.refreshAccounts();
    await fetchAccounts();
    return results;
  }

  async function signAll(): Promise<AccountActionResult[]> {
    const results = await api.signAccounts();
    await fetchAccounts();
    return results;
  }

  return {
    accounts,
    sorted,
    loading,
    error,
    fetchAccounts,
    createAccount,
    saveAccount,
    patchMeta,
    hasCookie,
    rememberCookie,
    deleteAccount,
    reorder,
    refreshAll,
    signAll,
  };
});
