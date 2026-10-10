import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    { path: "/overview", component: () => import("./views/OverviewView.vue") },
    { path: "/tasks", component: () => import("./views/TasksView.vue") },
    { path: "/accounts", component: () => import("./views/AccountsView.vue") },
    { path: "/settings", component: () => import("./views/SettingsView.vue") },
    { path: "/logs", component: () => import("./views/LogsView.vue") },
    { path: "/downloads", component: () => import("./views/DownloadsView.vue") },
    { path: "/chat", component: () => import("./views/ChatView.vue") },
  ],
});

export default router;
