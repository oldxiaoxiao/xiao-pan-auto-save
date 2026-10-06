import js from "@eslint/js";
import tseslint from "typescript-eslint";
import pluginVue from "eslint-plugin-vue";

export default [
  { ignores: ["dist/**", "node_modules/**", "*.config.js"] },
  {
    // __APP_VERSION__ 由 vite.config.ts 的 define 在构建期注入（src/env.d.ts 里也有 TS 声明），
    // 但 eslint 的 no-undef 只认 languageOptions.globals，不读 TS 声明——在这里如实登记，
    // 而不是去动 App.vue 或放宽规则。
    languageOptions: {
      globals: {
        __APP_VERSION__: "readonly",
      },
    },
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs["flat/recommended"],
  {
    files: ["**/*.vue"],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
        extraFileExtensions: [".vue"],
        sourceType: "module",
      },
    },
  },
  {
    rules: {
      "vue/multi-word-component-names": "off",
      "vue/require-default-prop": "off",
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
      "no-empty": ["error", { allowEmptyCatch: true }],
    },
  },
];
