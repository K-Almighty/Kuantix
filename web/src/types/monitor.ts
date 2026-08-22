/** monitor 路由 DTO（契约 §3.5） */

import type { Envelope } from './envelope';

export interface MonitorStatus {
  running: boolean;
  started_at: string | null;
  poll_interval_seconds: number;
  trading_hours_only: boolean;
  in_trading_session: boolean;
  last_poll_at: string | null;
  last_poll_ok: boolean | null;
  consecutive_errors: number;
  watchlist_count: number;
  rules_enabled_count: number;
  channels: ChannelInfo[];
}

export interface WatchlistItem {
  code: string;
  name: string;
  market: string;
  added_at: string;
  source: string;
}

export interface RuleScope {
  market: string;
  /** ['*'] = 全部 */
  codes: string[];
}

export type CriterionType = 'price' | 'indicator' | 'stop_loss';

export type AlertLevel = 'info' | 'warning' | 'critical';

export interface Rule {
  id: string;
  name: string;
  scope: RuleScope;
  criterion_type: CriterionType;
  params: Record<string, unknown>;
  level: AlertLevel;
  cooldown_seconds: number;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  last_triggered_at: string | null;
}

export interface RuleInput {
  name?: string;
  scope?: RuleScope;
  criterion_type?: CriterionType;
  params?: Record<string, unknown>;
  level?: AlertLevel;
  cooldown_seconds?: number;
  enabled?: boolean;
}

export interface CriterionInfo {
  type: string;
  display_name: string;
  description: string;
  params_schema: Record<string, unknown>;
}

export interface PositionInput {
  code: string;
  market: string;
  /** 数量（股，非手） */
  shares: number;
  cost_price: number;
  opened_at?: string | null;
}

export interface PositionView {
  code: string;
  name: string;
  market: string;
  shares: number;
  cost_price: number;
  last: number;
  /** 当日涨跌幅（比例，0.05=5%） */
  change_pct: number;
  market_value: number;
  pnl: number;
  /** 盈亏比例（比例） */
  pnl_pct: number;
  as_of: string;
}

export interface Alert {
  id: string;
  code: string;
  market: string;
  rule: string;
  level: AlertLevel;
  message: string;
  ts: string;
  payload: Record<string, unknown>;
}

export interface ChannelInfo {
  name: string;
  display_name: string;
  enabled: boolean;
  healthy: boolean | null;
}

export interface WatchlistAddResult {
  added: string[];
  skipped: { code: string; reason: string }[];
}

export interface RemoveResult {
  removed: string;
}

/* ---------- 预设监控规则（一键开关） ---------- */

export interface PresetStatus {
  key: string;
  name: string;
  description: string;
  criterion_type: string;
  params: Record<string, unknown>;
  level: AlertLevel;
  default_enabled: boolean;
  /** 是否已注入为真实规则 */
  applied: boolean;
  /** 当前是否启用（未注入为 null） */
  enabled: boolean | null;
  /** 已注入规则 id（未注入为 null） */
  rule_id: string | null;
}

/* ---------- WebSocket 协议（契约 §2.4.1 M17） ---------- */

export type WsMessageType = 'hello' | 'snapshot' | 'alert' | 'ping' | 'pong' | 'bye';

export interface WsFrameData {
  type: string;
  [key: string]: unknown;
}

/** 每条 WS 帧都是合法 NF-9 信封 */
export type WsEnvelope = Envelope<WsFrameData>;

export interface WsHelloPayload {
  market: string;
  subscribed: string[];
  server_ts: string;
}

export interface WsPongPayload {
  server_ts: string;
}

export interface WsByePayload {
  reason: string;
}
