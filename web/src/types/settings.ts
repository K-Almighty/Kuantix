/**
 * settings 路由 DTO（契约 §2.1f，v1.3 增量 P2；**只读**数据源状态，NF-20）。
 * - E1 GET /api/v1/settings/status —— 只读数据源状态；
 * - E2 POST /api/v1/settings/test-connection —— 主机连通性测试（只测不写）。
 */
import type { DataLakeStatus } from './data';

/** 客户端类型（对应三条独立链路：std=标准协议 / mac=A股行情 / mac_ex=港美股扩展） */
export type ClientKind = 'std' | 'mac' | 'mac_ex';

/** config.toml [tdx] 段摘要（E1） */
export interface TdxSummary {
  use_easy_tdx_known_hosts: boolean;
  port: number;
  ex_port: number;
  timeout_seconds: number;
  mac_hosts: string[];
  std_hosts: string[];
  mac_ex_hosts: string[];
}

/** config 摘要（E1）：数据路径 + 默认市场 + 端口（只展示，不写） */
export interface SettingsConfigSummary {
  paths: {
    root: string;
    vipdoc: string;
    factors: string;
    db: string;
    logs: string;
    reports: string;
    exports: string;
  };
  default_market: string;
  enabled_markets: string[];
  config_source: string;
  tdx: TdxSummary;
}

/** known_hosts 行（E1）：**只读展示**，read_only 恒为 true */
export interface SettingsKnownHostItem {
  host: string;
  port: number;
  kind: ClientKind;
  read_only: boolean;
}

/** known_hosts 汇总（E1）：config.toml 为主 + 上游 ~/.easy_tdx/config.json 只读合入 */
export interface SettingsKnownHosts {
  items: SettingsKnownHostItem[];
  upstream_available: boolean;
  known_hosts_merged: boolean;
  /** NF-20 自证：读取前后 sha256 指纹一致（Kuantix 从未写上游文件） */
  upstream_config_untouched: boolean;
  upstream_config_path: string;
}

/** E1 响应 data（SettingsStatus） */
export interface SettingsStatus {
  /** 整页只读声明（NF-20：Kuantix 不提供"切换服务器/保存配置"能力） */
  read_only: boolean;
  config: SettingsConfigSummary;
  known_hosts: SettingsKnownHosts;
  /** 数据湖摘要（复用 D1） */
  data: DataLakeStatus;
  versions: {
    Kuantix: string;
    upstream_easy_tdx: string;
  };
}

/** E2 请求体：显式 host/port/kind（禁 from_best_host） */
export interface TestConnectionRequest {
  kind: ClientKind;
  host: string;
  port: number;
}

/** E2 响应 data（TestConnectionResult）：ok=false 是**业务结果**，非 HTTP 错误 */
export interface TestConnectionResult {
  ok: boolean;
  host: string;
  port: number;
  kind: ClientKind;
  latency_ms: number | null;
  error: string | null;
}
