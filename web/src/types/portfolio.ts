/**
 * portfolio 路由 DTO（契约 v1.3 草案 §2.1 P1–P3，P0）。
 * 组合回测 = 1 策略 × N 标的，资金分仓（total_cash/N），combined_equity 按日期对齐金额求和。
 * 字段名/端点路径严格照抄契约草案，禁止自创。
 */
import type { BacktestEquityPoint, BacktestPerformance, BacktestTrade } from './backtest';

/** P1 请求体 PortfolioRunRequest（与 B2 字段集对齐，语义=总资金分仓） */
export interface PortfolioRunRequest {
  market: string;
  /** 1..20 组合标的池（6 位代码） */
  codes: string[];
  /** 单一策略（组合=1 策略 × N 标的） */
  strategy: string;
  params: Record<string, string | number | boolean>;
  start: string;
  end: string;
  /** 组合总资金，按 N 均分 */
  cash: number;
  commission: number;
  min_commission: number;
  stamp_tax: number;
  slippage: number;
  execution: 'next_open' | 'next_close';
}

/** P3 组合整体绩效（契约草案仅保证 4 字段；其余如 max_drawdown/sharpe 若后端补发则以索引读取） */
export interface PortfolioTotalPerformance {
  total_return: number | null;
  annual_return: number | null;
  total_stocks: number | null;
  total_cash: number | null;
  [key: string]: number | string | boolean | null;
}

/** P3 单标的/单槽位结果（对齐 B4 per_code 形状） */
export interface PortfolioIndividualResult {
  performance: BacktestPerformance;
  equity_curve: BacktestEquityPoint[];
  trades: BacktestTrade[];
  positions: unknown[];
  config: Record<string, unknown>;
}

/** P3 完整组合结果 PortfolioResult */
export interface PortfolioResult {
  total_performance: PortfolioTotalPerformance;
  /** key：组合回测=6 位 code；多策略组合回测="{label}@{symbol}" */
  individual_results: Record<string, PortfolioIndividualResult>;
  /** 资金分配（各 code/槽位占比，和为 1） */
  equity_allocation: Record<string, number>;
  /** 组合净值曲线（金额求和，含 drawdown_pct 供组合评级消费） */
  combined_equity: BacktestEquityPoint[];
}

/** P1 Job.result_summary（module=backtest, action=portfolio） */
export interface PortfolioJobSummary {
  strategy: string;
  codes: string[];
  result_count: number;
  skipped_count: number;
  total: {
    total_return: number | null;
    annual_return: number | null;
    total_stocks: number | null;
    total_cash: number | null;
    combined_points: number | null;
  };
}
