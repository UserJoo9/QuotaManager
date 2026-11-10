import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { rmSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { resolve } from "node:path";

// The build writes web/index.html + web/app/* (hashed files). The Python server
// keeps serving web/ as static files, so production needs no Node at all.
const WEB = resolve(__dirname, "../web");

// Only web/app/ is ours: wipe it before each build so old hashed files never pile up.
// (emptyOutDir stays off because web/ also holds the legacy pages, fonts and portals.)
const cleanAppDir = (): Plugin => ({
  name: "qm-clean-app-dir",
  apply: "build",
  buildStart() { rmSync(resolve(WEB, "app"), { recursive: true, force: true }); },
});

// Precompress once at build time; the server sends the .gz twin (no gzip work on the gateway).
const precompress = (): Plugin => ({
  name: "qm-precompress",
  apply: "build",
  closeBundle() {
    const dir = resolve(WEB, "app");
    for (const f of readdirSync(dir)) {
      if (!/\.(js|css|svg|json)$/.test(f)) continue;
      const raw = readFileSync(resolve(dir, f));
      if (raw.length < 1024) continue;
      const compressed = gzipSync(raw, { level: 9 });
      // RFC 1952 OS marker: unknown, so Windows and Linux emit identical headers.
      compressed[9] = 255;
      writeFileSync(resolve(dir, f + ".gz"), compressed);
    }
  },
});

export default defineConfig({
  plugins: [react(), tailwindcss(), cleanAppDir(), precompress()],
  base: "/",
  build: {
    outDir: WEB,
    emptyOutDir: false,
    assetsDir: "app",
    target: "es2020",
    sourcemap: false,
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    // dev: the Python API runs on 8080 (or PORT); Vite proxies API + WebSocket + legacy files
    proxy: Object.fromEntries(["/api", "/ws", "/js", "/css", "/fonts", "/assets", "/logo.svg"].map((p) => [p, {
      target: process.env.QM_API || "http://127.0.0.1:8080", ws: p === "/ws", changeOrigin: false,
    }])),
  },
  test: { environment: "jsdom", globals: false, include: ["src/**/*.test.{ts,tsx}"] },
} as never);
