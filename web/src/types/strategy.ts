/**
 * strategies 路由 DTO（契约 v1.3 草案 §2.2 S1–S5，P0）。
 * 策略库 CRUD + 多策略组合回测（N 策略 × 各自标的，资金 1/N，结果结构同 PortfolioResult）。
 * 字段名/端点路径严格照抄契约草案，禁止自创。
 */

/** 策略类型：single=单标的 / portfolio=组合（1 策略×N 标的）/ multi=多策略组合 */
export type StrategyKind = 'single' | 'portfolio' | 'multi';

/** S2 SavedStrategyCreate.context：随 kind 变化（single: symbol/日期；portfolio: stocks[]；multi: items[]） */
export interface SavedStrategyContext {
  /** single 时：如 "SH:600519"（前缀仅展示/标识，后端按 6 位 code + MarketProfile 解析） */
  symbol?: string;
  /** portfolio 时：6 位代码列表 */
  stocks?: string[];
  /** multi 时：策略槽位列表 */
  items?: Array<{
    strategy: string;
    label: string;
    code: string;
    params: Record<string, unknown>;
  }>;
  start_date?: string;
  end_date?: string;
  [key: string]: unknown;
}

export interface SavedStrategyTradeConfig {
  cash?: number;
  commission?: number;
  min_commission?: number;
  stamp_tax?: number;
  slippage?: number;
  execution?: string;
  [key: string]: unknown;
}

/** 保存时关键绩效快照（从回测结果自动填充；手工新建可为空对象） */
export interface SavedStrategySnapshot {
  total_return?: number | null;
  sharpe?: number | null;
  grade?: string | null;
  [key: string]: unknown;
}

/** S2 请求体 */
export interface SavedStrategyCreate {
  name: string;
  kind: StrategyKind;
  strategy: string;
  strategy_label: string;
  params: Record<string, string | number | boolean>;
  context: SavedStrategyContext;
  trade_config: SavedStrategyTradeConfig;
  snapshot: SavedStrategySnapshot;
  tags: string[];
  notes: string;
}

/** 服务端返回的已存策略（含服务端生成字段） */
export interface SavedStrategy extends SavedStrategyCreate {
  id: string;
  created_at: string;
  updated_at: string;
  app_version: string;
}

/** S5 多策略槽位 */
export interface MultiStrategySlot {
  strategy: string;
  label: string;
  code: string;
  params: Record<string, string | number | boolean>;
}

/** S5 请求体 MultiStrategyRunRequest */
export interface MultiStrategyRunRequest {
  market: string;
  /** N 个策略槽位（1..10） */
  items: MultiStrategySlot[];
  /** 总资金 1/N 均分到各槽位 */
  cash: number;
  commission: number;
  min_commission: number;
  stamp_tax: number;
  slippage: number;
  execution: 'next_open' | 'next_close';
  start: string;
  end: string;
}
