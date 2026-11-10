import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
// Keep the deployed dashboard untouched while the React migration is reviewed.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "../.contrib-build", emptyOutDir: true, target: "es2020" },
  server: { proxy: Object.fromEntries(["/api", "/ws", "/css", "/fonts", "/logo.svg"].map((p) => [p, { target: process.env.QM_API || "http://127.0.0.1:8080", ws: p === "/ws", changeOrigin: false }])) },
  test: { environment: "jsdom", globals: false, include: ["src/**/*.test.{ts,tsx}"] },
} as never);
