import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  build: {
    outDir: "dist/client",
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          if (/markdown-it|marked\/|linkify-it|mdurl|punycode/.test(id))
            return "markdown-renderer";
          if (
            /@tiptap|prosemirror|orderedmap|rope-sequence|w3c-keyname/.test(id)
          )
            return "editor-engine";
        },
      },
    },
  },
  optimizeDeps: {
    include: ["react", "react-dom/client"],
  },
  server: {
    port: 5178,
    strictPort: true,
    proxy: {
      "/api/v1": { target: process.env.FKB_DEV_API_TARGET || "http://127.0.0.1:8765", changeOrigin: false },
    },
    host: "127.0.0.1",
    warmup: {
      clientFiles: ["./src/main.jsx"],
    },
  },
  plugins: [react()],
});
