import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // Splits the one enormous bundle Vite's own build warning was
        // flagging into pieces the browser can actually use well: react/
        // react-dom almost never change between deploys of this app, so
        // giving them their own chunk means a returning visitor's browser
        // can keep serving that chunk from cache across most updates
        // instead of re-downloading it every time any app code changes.
        // @react-google-maps/api is a sizeable wrapper only the routes/
        // map screens ever touch - giving it its own chunk at least keeps
        // it a distinct, independently-cacheable file rather than fused
        // into the one main chunk (RouteWorkspace itself stays eagerly
        // bundled rather than React.lazy-loaded: App.jsx mounts it
        // unconditionally on first render - see "Boards - all always
        // mounted" there - so lazy-loading only it wouldn't skip loading
        // it, just delay the same work by one extra round trip). exceljs
        // (the other major weight in this app) already left the bundle
        // entirely via a dynamic import in App.jsx, so it doesn't need a
        // manual chunk here at all - Vite gives it one automatically.
        manualChunks: {
          'vendor-react': ['react', 'react-dom'],
          'vendor-google-maps': ['@react-google-maps/api'],
        },
      },
    },
  },
  server: {
    host: '0.0.0.0',
    port: 3000,
    allowedHosts: ['.trycloudflare.com'],
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
      '/upload-excel': 'http://127.0.0.1:8000',
      '/generate-routes': 'http://127.0.0.1:8000',
      // Plain string proxy entries above only handle HTTP - a WebSocket
      // upgrade request needs `ws: true` explicitly, or Vite's dev proxy
      // never forwards the Upgrade handshake to the backend and the
      // browser's WebSocket just fails to connect.
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
});
