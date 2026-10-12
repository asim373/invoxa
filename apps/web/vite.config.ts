import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";
import { validateProductionApiUrl, validateProductionAppUrl } from "./src/production-config.ts";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  if (mode === "production") {
    validateProductionApiUrl(env.VITE_API_BASE_URL);
    validateProductionAppUrl(env.VITE_PUBLIC_APP_URL);
  }
  return {
    plugins: [react()],
    server: {
      host: "0.0.0.0",
      port: 5173,
      watch: {
        usePolling: true,
        interval: 1000,
      },
    },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: "./src/test-setup.ts",
      testTimeout: 15000,
    },
  };
});
