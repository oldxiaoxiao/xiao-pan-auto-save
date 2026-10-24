import { createApp } from "vue";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import "element-plus/dist/index.css";
import AuthGate from "./components/AuthGate.vue";
import { useErrorsStore } from "./stores/errors";
import router from "./router";
import "./styles/theme.css";

/**
 * 界面错误一律**说出口**，不许静默。
 *
 * Vue 默认把渲染/事件错误只打到控制台——打包后的客户端没有控制台可看，
 * 一个组件渲染失败就会表现为"页面从此点什么都没反应"，用户拿不到任何线索。
 * 这里统一登记到 errors store（界面底部会显示，可复制原文），并弹一条提示。
 */
const app = createApp(AuthGate);
const pinia = createPinia();
app.use(pinia);
app.use(router);
app.use(ElementPlus, { locale: zhCn });
setActivePinia(pinia);

const errors = useErrorsStore();
app.config.errorHandler = (err, _instance, info) => errors.report(err, `组件：${info}`);
window.addEventListener("error", (event) => errors.report(event.error ?? event.message, "window"));
window.addEventListener("unhandledrejection", (event) => errors.report(event.reason, "未处理的 Promise 拒绝"));

app.mount("#app");
