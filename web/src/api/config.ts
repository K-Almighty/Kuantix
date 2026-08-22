/**
 * API 基础配置（契约 §1.1）。
 * 后端 Base URL：端口不硬编码，默认 http://127.0.0.1:8899/api/v1 仅作配置项，
 * 由 VITE_API_BASE 环境变量注入/覆盖（以 config.toml [server] 为准）。
 */

export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) || 'http://127.0.0.1:8899/api/v1';

/** 服务器 Origin（用于 /api/version、/health 基础设施端点） */
export function serverOrigin(): string {
  return API_BASE.replace(/\/api\/v1\/?$/, '');
}
