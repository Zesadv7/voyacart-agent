import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // 共享凭据不得进入构建产物；只允许运行时登录。
  const env = loadEnv(mode, process.cwd(), "");
  if (process.env.VITE_API_TOKEN || env.VITE_API_TOKEN)
    throw new Error("VITE_API_TOKEN is forbidden. Use runtime short-lived identity login.");
  return {
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          if (/\/(?:@ag-ui|rxjs|zod|fast-json-patch)\//.test(id))
            return "agent";
          return "ui-vendor";
        },
      },
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/commerce": {
        target: process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true,
        ws: true,
      },
      "/health": {
        target: process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  };
});
