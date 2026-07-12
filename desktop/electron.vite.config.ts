import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, externalizeDepsPlugin } from "electron-vite";

const pkg = JSON.parse(readFileSync(resolve(__dirname, "package.json"), "utf-8")) as {
  version: string;
};
const define = { __APP_VERSION__: JSON.stringify(pkg.version) };

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    define,
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    define,
  },
  renderer: {
    plugins: [react(), tailwindcss()],
    define,
    resolve: {
      alias: {
        "@": resolve(__dirname, "src/renderer/src"),
      },
    },
  },
});
