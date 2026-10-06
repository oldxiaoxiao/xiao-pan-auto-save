<script setup lang="ts">
import { ref, watch, reactive } from "vue";
import { ElMessage } from "element-plus";
import { useSettingsStore } from "../stores/settings";
import type { SearchSource } from "../api/types";

const store = useSettingsStore();
const saving = ref(false);
const form = reactive({
  pansou_server: "",
  pansou_enable: true,
  cs_server: "",
  cs_username: "",
  cs_password: "",
  cs_token: "",
  cs_enable: true,
});

function syncFrom(v: SearchSource) {
  const ps = (v?.pansou ?? {}) as Record<string, unknown>;
  const cs = (v?.cloudsaver ?? {}) as Record<string, unknown>;
  form.pansou_server = String(ps.server ?? "");
  form.pansou_enable = String(ps.enable ?? "true").toLowerCase() !== "false";
  form.cs_server = String(cs.server ?? "");
  form.cs_username = String(cs.username ?? "");
  form.cs_password = String(cs.password ?? "");
  form.cs_token = String(cs.token ?? "");
  form.cs_enable = String(cs.enable ?? "true").toLowerCase() !== "false";
}

watch(
  // settings 是 Settings | null：syncFrom 收 undefined 也照样填空表单，值一到就同步。
  () => store.settings?.source,
  (v) => syncFrom(v ?? {}),
  { immediate: true, deep: true },
);

async function save() {
  saving.value = true;
  const payload: SearchSource = {
    pansou: { server: form.pansou_server, enable: form.pansou_enable },
    cloudsaver: {
      server: form.cs_server,
      username: form.cs_username,
      password: form.cs_password,
      token: form.cs_token,
      enable: form.cs_enable,
    },
  };
  try {
    await store.save("source", payload);
    ElMessage.success("搜索源已保存");
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
      <h3>资源搜索源</h3>
      <el-button type="primary" size="small" :loading="saving" @click="save"> 保存 </el-button>
    </div>

    <div class="grp card">
      <div class="grp__top">
        <span>PanSou</span>
        <el-switch v-model="form.pansou_enable" size="small" />
      </div>
      <label class="field-label">服务器地址</label>
      <el-input v-model="form.pansou_server" placeholder="https://so.252035.xyz" />
    </div>

    <div class="grp card">
      <div class="grp__top">
        <span>CloudSaver</span>
        <el-switch v-model="form.cs_enable" size="small" />
      </div>
      <div class="two">
        <div>
          <label class="field-label">服务器</label>
          <el-input v-model="form.cs_server" placeholder="https://cloudsaver.example.com" />
        </div>
        <div>
          <label class="field-label">Token</label>
          <el-input v-model="form.cs_token" placeholder="留空，登录后自动写入" />
        </div>
        <div>
          <label class="field-label">用户名</label>
          <el-input v-model="form.cs_username" />
        </div>
        <div>
          <label class="field-label">密码</label>
          <el-input v-model="form.cs_password" type="password" show-password />
        </div>
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
.grp {
  padding: 14px;
}
.grp__top {
  display: flex;
  justify-content: space-between;
  font-weight: 600;
  margin-bottom: 10px;
}
.two {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
}
</style>
