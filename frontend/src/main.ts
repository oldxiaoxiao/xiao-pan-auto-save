import { createApp } from "vue";
import { createPinia } from "pinia";
import ElementPlus from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import "element-plus/dist/index.css";
import AuthGate from "./components/AuthGate.vue";
import router from "./router";
import "./styles/theme.css";

createApp(AuthGate).use(createPinia()).use(router).use(ElementPlus, { locale: zhCn }).mount("#app");
