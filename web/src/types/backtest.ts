/** backtest 路由 DTO（契约 §3.6，v1.2 增量） */

import type { Job } from './data';

export interface BacktestParamSchema {
  name: string;
  type: 'int' | 'float' | 'bool' | 'str';
  default: string | number | boolean | null;
  label: string;
  min_value?: number;
  max_value?: number;
  choices?: string[];
  description?: string;
}

export interface BacktestStrategySchema {
  name: string;
  label: string;
  description: string;
  params: BacktestParamSchema[];
  /** 寻优预设网格（B1 响应已含，契约 v1.3 P1 声明；供 OptimizeView ParamGridPicker 消费） */
  preset_grid?: Record<string, Array<number | string>>;
}

export interface BacktestRunRequest {
  market: string;
  codes: string[];
  strategy: string;
  params: Record<string, string | number | boolean>;
  start: string;
  end: string;
  cash: number;
  commission: number;
  min_commission: number;
  stamp_tax: number;
  slippage: number;
  execution: 'next_open' | 'next_close';
}

export interface BacktestEquityPoint {
  datetime: string;
  total: number;
  drawdown: number;
  drawdown_pct?: number;
}

export interface BacktestTrade {
  datetime: number | string;
  direction: string;
  size: number;
  price: number;
  commission: number;
  slippage: number;
  pnl: number;
  cost_basis: number;
  rejected: boolean;
}

export interface BacktestPerformance {
  [key: string]: number | null;
}

export interface BacktestPerCodeResult {
  performance: BacktestPerformance;
  equity_curve: BacktestEquityPoint[];
  trades: BacktestTrade[];
  positions: unknown[];
  config: Record<string, unknown>;
  diagnostic: string | null;
}

export interface BacktestCombinedResult {
  equity_curve: BacktestEquityPoint[];
  performance: BacktestPerformance;
  config: Record<string, unknown>;
}

export interface BacktestSkippedCode {
  code: string;
  reason: string;
}

export interface BacktestResult {
  strategy: string;
  params: Record<string, string | number | boolean>;
  market: string;
  start_date: string;
  end_date: string;
  config: {
    cash: number;
    commission: number;
    min_commission: number;
    stamp_tax: number;
    slippage: number;
    execution: string;
  };
  codes: string[];
  skipped: BacktestSkippedCode[];
  per_code: Record<string, BacktestPerCodeResult>;
  combined: BacktestCombinedResult;
}

/** B3 结果摘要（Job.result_summary） */
export interface BacktestJobSummary {
  strategy: string;
  market: string;
  codes: string[];
  result_count: number;
  skipped_count: number;
  combined: {
    total_return: number | null;
    annual_return: number | null;
    max_drawdown: number | null;
    sharpe: number | null;
    total_trades: number | null;
    win_rate: number | null;
    equity_points: number | null;
  };
}

/* -------- K 线下钻（P1，GET /api/v1/backtest/kline/{code}，契约 §3.8 B5） -------- */

/** 单根 K 线（日线 OHLCV + 成交额） */
export interface KlineBar {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  vol: number;
  amount: number;
}

/** 买卖点信号标注（契约 §3.8 SignalPoint；price 为 null 时前端取当日高低价占位） */
export interface SignalPoint {
  date: string;
  price: number | null;
}

/** B5 KlineWithSignals：K 线数组 + 买卖点信号标注（非下单动作，R5） */
export interface KlineWithSignals {
  code: string;
  market: string;
  start_date: string;
  end_date: string;
  /** 买卖点标注所用策略 */
  strategy: string;
  /** K 线数组（升序） */
  kline: KlineBar[];
  buy_points: SignalPoint[];
  sell_points: SignalPoint[];
}

/* -------- C1 回测任务列表（P1，GET /api/v1/backtest/jobs） -------- */

/** C1 响应载荷：{items: [Job], count}（limit 1..50，默认 20，created_at 倒序） */
export interface BacktestJobList {
  items: Job[];
  count: number;
}
