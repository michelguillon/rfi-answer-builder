import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// ARCHITECTURAL DECISION: Vite proxy for /api, not CORS on FastAPI.
//
// The browser talks to the frontend at http://localhost:3000; the
// frontend code uses relative /api/... paths; Vite dev server (running
// inside the frontend container) proxies those to the backend service.
// Inside docker compose the target is http://backend:8000; outside
// compose (rare: someone running `npm run dev` on the host) it falls
// back to http://localhost:8000.
//
// This shape works identically in production behind any reverse proxy
// (Nginx, Caddy, the user's existing SSO gateway) that forwards /api
// to the FastAPI port. No CORS headers ever, no preflight roundtrips,
// no /api-vs-/ origin mismatch in the browser console. The cost is a
// few lines of proxy config — much cheaper than maintaining a CORS
// allowlist that has to grow every time the deployment URL changes.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    host: "0.0.0.0",
    port: 3000,
    proxy: {
      "/api": {
        target: process.env.VITE_API_URL || "http://localhost:8000",
        changeOrigin: true,
      },
      "/healthz": {
        target: process.env.VITE_API_URL || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
