import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The browser calls /api/*, Vite forwards it to FastAPI (no CORS setup needed in dev).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
