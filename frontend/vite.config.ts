import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The API is local-demo only (MOCK). The proxy forwards the browser's Origin and Host
// unchanged (changeOrigin stays false), so the backend's Origin and loopback Host checks still apply.
const proxy = {
  '/api': {
    target: 'http://127.0.0.1:8765',
    changeOrigin: false,
  },
};

export default defineConfig({
  plugins: [react()],
  server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
  preview: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
});
