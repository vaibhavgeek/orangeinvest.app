import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, /api is proxied to the Python backend. In production set VITE_API_URL to the backend's origin.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": process.env.BACKEND_URL || "http://127.0.0.1:8000" } },
});
