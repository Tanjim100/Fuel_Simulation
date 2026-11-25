import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * The dev server proxies the API to the backend so the browser talks to a
 * single origin. In the container the same proxy runs in front of the built
 * assets (see `server.mjs`), so the frontend never needs CORS configuration
 * and never learns the backend's internal hostname.
 */
const BACKEND = process.env.BACKEND_ORIGIN ?? 'http://localhost:8002'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: BACKEND,
        changeOrigin: true,
        // SSE needs buffering disabled; Vite's proxy is a plain http-proxy
        // stream and does not buffer, but the header makes the intent explicit.
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('Accept-Encoding', 'identity')
          })
        },
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    // The default 5s per-test budget is too short for `live.test.tsx`, which
    // renders the whole console against the running backend: nine parallel
    // REST reads plus the first SSE connection have to land before any panel
    // has data. Scoped to that file rather than raised globally, so the fast
    // unit tests keep their tight budget.
    testTimeout: 10_000,
    hookTimeout: 10_000,
  },
})
