<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "./api/client";

const nav = [
  { to: "/tasks", label: "任务", icon: "M3 5h18M3 12h18M3 19h18" },
  { to: "/accounts", label: "账号", icon: "M16 20v-2a4 4 0 0 0-8 0v2M12 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" },
  { to: "/settings", label: "设置", icon: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM4 12h2m12 0h2" },
  { to: "/logs", label: "日志", icon: "M4 6h16M4 12h16M4 18h10" },
];

const backendDown = ref(false);

async function checkHealth() {
  try {
    const r = await api.health();
    backendDown.value = r.status !== "ok";
  } catch {
    backendDown.value = true;
  }
}

onMounted(() => {
  checkHealth();
  window.addEventListener("focus", checkHealth);
});
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand__mark">盘</span>
        <span class="brand__text">小盘自动转存</span>
      </div>
      <nav>
        <RouterLink v-for="item in nav" :key="item.to" :to="item.to" class="nav-item">
          <svg
            viewBox="0 0 24 24"
            width="18"
            height="18"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
          >
            <path :d="item.icon" />
          </svg>
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>
      <div class="sidebar__foot">
        <span class="dot" :class="backendDown ? 'dot--off' : 'dot--on'" />
        {{ backendDown ? "后端未连接" : "已连接" }}
      </div>
    </aside>

    <header class="mobile-bar">
      <span class="brand__mark">盘</span>
      <RouterLink v-for="item in nav" :key="item.to" :to="item.to" class="mtab">
        {{ item.label }}
      </RouterLink>
    </header>

    <main class="content">
      <div v-if="backendDown" class="offline">
        无法连接到后端服务，请先启动：<code>uvicorn backend.main:app --port 8432</code>
      </div>
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  min-height: 100vh;
}
.sidebar {
  width: 210px;
  background: #fff;
  border-right: 1px solid var(--border);
  padding: 22px 14px;
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-left: 8px;
  margin-bottom: 26px;
}
.brand__mark {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: var(--primary);
  color: #fff;
  font-weight: 700;
  display: grid;
  place-items: center;
  font-size: 15px;
}
.brand__text {
  font-weight: 700;
  font-size: 15px;
}
.sidebar nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  color: #5b6270;
  font-size: 14px;
}
.nav-item:hover {
  background: #f2f4f7;
}
.nav-item.router-link-active {
  background: var(--primary-soft);
  color: var(--primary);
  font-weight: 600;
}
.sidebar__foot {
  margin-top: auto;
  font-size: 12px;
  color: var(--text-muted);
  display: flex;
  align-items: center;
  gap: 6px;
  padding-left: 8px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.dot--on {
  background: var(--success);
}
.dot--off {
  background: var(--danger);
}

.mobile-bar {
  display: none;
}

.content {
  flex: 1;
  padding: 24px;
  min-width: 0;
}
.offline {
  background: #fdeceb;
  color: var(--danger);
  border: 1px solid #f7cbc9;
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 16px;
  font-size: 13px;
}
.offline code {
  background: #fff;
  padding: 1px 6px;
  border-radius: 4px;
}

@media (max-width: 767px) {
  .sidebar {
    display: none;
  }
  .content {
    padding: 16px;
  }
  .mobile-bar {
    display: flex;
    align-items: center;
    gap: 4px;
    position: sticky;
    top: 0;
    z-index: 30;
    background: #fff;
    border-bottom: 1px solid var(--border);
    padding: 8px 10px;
    overflow-x: auto;
  }
  .mtab {
    flex: 1;
    text-align: center;
    padding: 8px 10px;
    border-radius: 8px;
    font-size: 14px;
    color: #5b6270;
    white-space: nowrap;
  }
  .mtab.router-link-active {
    background: var(--primary-soft);
    color: var(--primary);
    font-weight: 600;
  }
  .layout {
    flex-direction: column;
  }
}
</style>
