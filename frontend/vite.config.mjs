import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'node:fs'

const TRANSIENT_PROXY_CODES = new Set([
  'ECONNREFUSED',
  'ECONNRESET',
  'ETIMEDOUT',
  'EAI_AGAIN',
  'ENOTFOUND',
]);

function writeProxyErrorResponse(res, code) {
  if (!res || typeof res.writeHead !== 'function' || res.headersSent) {
    return;
  }

  const isTransient = TRANSIENT_PROXY_CODES.has(code);
  res.writeHead(isTransient ? 503 : 502, {
    'Content-Type': 'application/json',
    'Cache-Control': 'no-store',
  });
  res.end(
    JSON.stringify({
      detail: isTransient
        ? 'Backend temporarily unavailable. Please retry in a few seconds.'
        : 'Proxy request failed.',
      code,
    })
  );
}

function attachProxyLogging(proxy, label, shouldWriteResponse = false) {
  proxy.on('error', (err, req, res) => {
    const code = err && typeof err === 'object' && 'code' in err ? String(err.code) : 'UNKNOWN';
    const requestPath = req?.url || '';
    console.warn(`[vite-proxy:${label}] ${code} ${requestPath}`);

    if (shouldWriteResponse) {
      writeProxyErrorResponse(res, code);
    }
  });
}

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  let target = process.env.VITE_BACKEND_URL || env.VITE_BACKEND_URL || 'http://127.0.0.1:8001';
  const envDocker = (process.env.VITE_DOCKER || env.VITE_DOCKER || '').toLowerCase() === 'true';
  const isDockerRuntime = envDocker || fs.existsSync('/.dockerenv');

  // If Docker-style hostname leaks into a local run, fallback to host-mapped backend.
  if (!isDockerRuntime) {
    try {
      const parsed = new URL(target);
      if (parsed.hostname === 'backend' || parsed.hostname === 'localhost') {
        target = 'http://127.0.0.1:8001';
      }
    } catch (_err) {
      console.warn(`Invalid VITE_BACKEND_URL "${target}", falling back to http://127.0.0.1:8001`);
      target = 'http://127.0.0.1:8001';
    }
  }
  
  console.log(`Proxying /api to: ${target}`);

  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      watch: {
        usePolling: true,
        interval: 300,
      },
      proxy: {
        '/api/stream': {
          target: target,
          changeOrigin: true,
          secure: false,
          // Keep SSE stream open; generic API timeout causes chunked stream aborts.
          timeout: 0,
          proxyTimeout: 0,
          configure: (proxy, _options) => {
            attachProxyLogging(proxy, 'sse', true);
          },
        },
        '/api': {
          target: target,
          changeOrigin: true,
          secure: false,
          timeout: 10000,
          proxyTimeout: 10000,
          configure: (proxy, _options) => {
            attachProxyLogging(proxy, 'api', true);
          },
        },
        '/ws': {
          target: target,
          changeOrigin: true,
          secure: false,
          ws: true,
          configure: (proxy, _options) => {
            attachProxyLogging(proxy, 'ws');
          },
        }
      }
    },
  }
})
