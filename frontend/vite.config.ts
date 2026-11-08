import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Local demo API proxy. Default: the backend API on 127.0.0.1:8765 (unchanged).
// SETTLEWELL_API_PROXY_TARGET may override it, but only with plain http://127.0.0.1:<port>
// or http://localhost:<port> (no userinfo, path, query or hash; no remote hosts).
// The proxy forwards the browser's Origin and Host unchanged (changeOrigin stays false),
// so the backend's Origin and loopback Host checks still apply.
const DEFAULT_TARGET = 'http://127.0.0.1:8765';
const TARGET_PATTERN = /^http:\/\/(127\.0\.0\.1|localhost):(\d{1,5})\/?$/;

function resolveProxyTarget(raw: string | undefined): string {
  if (raw === undefined || raw.trim() === '') return DEFAULT_TARGET;
  const match = TARGET_PATTERN.exec(raw.trim());
  const port = match ? Number(match[2]) : NaN;
  if (!match || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(
      'SETTLEWELL_API_PROXY_TARGET must be http://127.0.0.1:<port> or http://localhost:<port> (local only)',
    );
  }
  return `http://${match[1]}:${port}`;
}

const proxy = {
  '/api': {
    target: resolveProxyTarget(process.env.SETTLEWELL_API_PROXY_TARGET),
    changeOrigin: false,
  },
};

export default defineConfig({
  plugins: [react()],
  server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
  preview: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
});
