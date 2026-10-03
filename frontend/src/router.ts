import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/tasks" },
    { path: "/tasks", component: () => import("./views/TasksView.vue") },
    { path: "/accounts", component: () => import("./views/AccountsView.vue") },
    { path: "/settings", component: () => import("./views/SettingsView.vue") },
    { path: "/logs", component: () => import("./views/LogsView.vue") },
  ],
});

export default router;
