import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    target: "es2022",
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
    // Single-page viewer: keep asset names predictable for local static serving.
    assetsDir: "assets",
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
  server: {
    host: "127.0.0.1",
    // Dev proxy targets the default viewer port; daemon may bump +0..19 if busy.
    proxy: {
      "/api": "http://127.0.0.1:47831",
    },
  },
});
