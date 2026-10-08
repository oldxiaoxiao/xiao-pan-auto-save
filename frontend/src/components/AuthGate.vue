<script setup lang="ts">
import { onMounted, ref } from "vue";
import App from "../App.vue";

const ready = ref(false);
const loggedIn = ref(false);
const required = ref(false);
const username = ref("admin");
const password = ref("");
const busy = ref(false);
const error = ref("");

async function check() {
  error.value = "";
  try {
    const response = await fetch("/api/auth/status", { cache: "no-store" });
    if (!response.ok) throw new Error("无法连接到后端服务");
    const state = await response.json();
    required.value = state.required;
    loggedIn.value = state.authenticated;
    ready.value = true;
  } catch (e) {
    error.value = (e as Error).message;
  }
}

async function login() {
  busy.value = true;
  error.value = "";
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ username: username.value, password: password.value }),
    });
    if (!response.ok) {
      const body = await response.json();
      throw new Error(body.detail || "登录失败");
    }
    password.value = "";
    await check();
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}

async function logout() {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.reload();
}

onMounted(check);
</script>

<template>
  <template v-if="ready && loggedIn">
    <button v-if="required" class="logout" @click="logout">退出登录</button>
    <App />
  </template>
  <main v-else class="login-page">
    <form class="login-card" @submit.prevent="login">
      <img src="/favicon.svg" alt="小盘" width="48" />
      <h1>小盘自动转存</h1>
      <p>登录后管理账号、追更任务和下载。</p>
      <template v-if="ready">
        <label>用户名<input v-model="username" autocomplete="username" required /></label>
        <label>密码<input v-model="password" type="password" autocomplete="current-password" required /></label>
        <button type="submit" :disabled="busy">{{ busy ? "登录中…" : "登录" }}</button>
      </template>
      <button v-else type="button" @click="check">重新连接</button>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </form>
  </main>
</template>

<style scoped>
.login-page {
  display: grid;
  place-items: center;
  min-height: 100vh;
  padding: 24px;
  background: #f5f7fb;
}
.login-card {
  width: min(100%, 380px);
  padding: 32px;
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 8px 40px #172b4d12;
}
h1 {
  font-size: 22px;
  margin: 16px 0 8px;
}
p {
  color: #667085;
  font-size: 14px;
}
label {
  display: grid;
  gap: 8px;
  margin-top: 20px;
  font-size: 14px;
}
input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid #d0d5dd;
  border-radius: 6px;
  font: inherit;
}
button {
  cursor: pointer;
  border: 0;
  border-radius: 6px;
  background: #3370ff;
  color: #fff;
  padding: 11px 16px;
}
.login-card button {
  width: 100%;
  margin-top: 24px;
}
.error {
  color: #d92d20;
}
.logout {
  position: fixed;
  right: 20px;
  bottom: 12px;
  z-index: 50;
  background: #fff;
  color: #667085;
  border: 1px solid #d0d5dd;
  padding: 5px 10px;
}
</style>
