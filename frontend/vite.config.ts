import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  build: { outDir: "dist" },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8432", changeOrigin: true },
    },
  },
});
