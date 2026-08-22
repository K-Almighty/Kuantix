import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// Kuantix 前端独立应用。后端地址不在此硬编码：
// 通过环境变量 VITE_API_BASE 注入（默认 http://127.0.0.1:8899/api/v1，见 .env*），
// 端口以 config.toml [server] 为准（契约 §1.1），前端只读配置。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    host: '127.0.0.1',
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
});
