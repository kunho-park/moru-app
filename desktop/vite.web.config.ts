/**
 * Renderer-only dev server (`bunx vite --config vite.web.config.ts`).
 * Runs the app in a plain browser via the lib/bridge.ts shim; pass
 * ?port=&token= to point it at a manually started engine sidecar.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const pkg = JSON.parse(readFileSync(resolve(__dirname, "package.json"), "utf-8")) as {
  version: string;
};

export default defineConfig({
  root: resolve(__dirname, "src/renderer"),
  plugins: [react(), tailwindcss()],
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  resolve: {
    alias: {
      "@": resolve(__dirname, "src/renderer/src"),
    },
  },
});
