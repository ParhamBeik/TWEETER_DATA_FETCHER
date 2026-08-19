import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        // The E2E run starts its own backend on a non-default port so it cannot
        // collide with (and silently drive) another project's dev server.
        target: `http://127.0.0.1:${process.env.E2E_API_PORT || 8000}`,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.js"],
    // e2e/ is Playwright's; vitest must not try to collect those specs.
    exclude: ["node_modules/**", "dist/**", "e2e/**"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{js,jsx}"],
      exclude: ["src/main.jsx", "src/test/**"],
    },
  },
});
