import { defineConfig } from "vite";
import { resolve } from "path";

export default defineConfig({
  root: ".",
  build: {
    outDir: resolve(__dirname, "../src/ziva/transports/desktop_api/static"),
    emptyOutDir: true,
    rollupOptions: {
      input: resolve(__dirname, "index.html"),
    },
  },
  server: {
    proxy: {
      "/sessions": "http://127.0.0.1:4097",
      "/automations": "http://127.0.0.1:4097",
      "/status": "http://127.0.0.1:4097",
      "/config": "http://127.0.0.1:4097",
      "/api": "http://127.0.0.1:4097",
    },
  },
});
