import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies the API to the backend so the frontend can run on
// :5173 with hot reload while talking to a container on :4533.
const backend = process.env.VITE_BACKEND_URL || 'http://localhost:4533'

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/rest': { target: backend, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
})
