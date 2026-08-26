import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";
import path from "path";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");

  return {
    plugins: [react(), tsconfigPaths()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "src"),
      },
    },
    server: {
      host: "0.0.0.0",
      port: 4173,
      proxy: {
        // Dev only. In production FastAPI serves both the API and this SPA
        // from one origin, so there is no proxy and no CORS at all.
        // Don't rewrite the path — the backend expects the /api prefix.
        "/api": {
          target: process.env.API_BASE_URL || "http://127.0.0.1:4174",
          changeOrigin: true,
        },
      },
      allowedHosts: env.VITE_ALLOWED_HOSTS?.split(",") || [],
    },
    build: {
      sourcemap: false,
    },
    preview: {
      allowedHosts: env.VITE_ALLOWED_HOSTS?.split(",") || [],
    },
  };
});
