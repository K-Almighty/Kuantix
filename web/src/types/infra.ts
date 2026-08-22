/** 基础设施端点 DTO（契约 §2.0） */

export interface VersionInfo {
  name: string;
  version: string;
  upstream_easy_tdx: string;
  config_source: string;
  market_default: string;
}

export interface HealthInfo {
  status: string;
  started_at: string;
  uptime_seconds: number;
  /** 后端可能返回 Record<市场码, bool> 或已启用市场数组；前端兼容两种形态 */
  markets_enabled: Record<string, boolean> | string[];
}
