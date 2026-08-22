/**
 * optimize 路由 DTO（契约 v1.3 P1，docs/06 §2.3 O1–O3）。
 * 单策略参数网格寻优：O1 提交 → Job（module=backtest, action=optimize）→ O2 进度 → O3 结果。
 * 字段名/端点路径严格照抄契约草案，禁止自创。
 */

/** O1 请求体 OptimizeRunRequest（单标的 + 1-2 参数笛卡尔积网格，≤200 点） */
export interface OptimizeRunRequest {
  market: string;
  /** 单标的 6 位代码（上游 O-1~O-6 为单标的寻优） */
  code: string;
  strategy: string;
  /** 1-2 个参数，各填候选值数组；笛卡尔积点数 ≤ 200（后端二次校验，超限 400） */
  param_grid: Record<string, Array<number | string>>;
  start: string;
  end: string;
  cash: number;
  commission: number;
  min_commission: number;
  stamp_tax: number;
  slippage: number;
  execution: 'next_open' | 'next_close';
}

/** 寻优网格点结果（O3 results 元素 / best，契约 §3.8 OptimizeGridPoint） */
export interface OptimizeGridPoint {
  params: Record<string, number | string>;
  total_return: number | null;
  sharpe: number | null;
  max_drawdown: number | null;
  total_trades: number;
  win_rate: number | null;
  profit_factor: number | null;
}

/** 2 参数时的热力图（契约 §3.8：{x_name, y_name, x, y, data}，data 为稀疏三元组 [x_idx, y_idx, value]） */
export interface OptimizeHeatmap {
  x_name: string;
  y_name: string;
  x: Array<number | string>;
  y: Array<number | string>;
  /** 稀疏三元组 [x_idx, y_idx, value]（ECharts heatmap 原生数据格式） */
  data: Array<[number, number, number | null]>;
}

/** O3 完整寻优结果 OptimizeResult（后端另附 market/code/start_date/end_date/config，前端按需忽略） */
export interface OptimizeResult {
  strategy: string;
  param_names: string[];
  /** 按 total_return 降序 */
  results: OptimizeGridPoint[];
  best: OptimizeGridPoint | null;
  /** 2 参数时非 null；单参数时为 null（前端改画折线/柱状） */
  heatmap: OptimizeHeatmap | null;
}

/** O1 Job.result_summary（module=backtest, action=optimize，对齐后端轻量摘要 {action, market, code, strategy, grid_size, param_names, result_count, best}） */
export interface OptimizeJobSummary {
  action: string;
  market: string;
  code: string;
  strategy: string;
  grid_size: number;
  param_names: string[];
  result_count: number;
  best: OptimizeGridPoint | null;
}

/** O4 请求体 OptimizeAllRunRequest（一键寻优所有策略） */
export interface OptimizeAllRunRequest {
  market: string;
  code: string;
  start: string;
  end: string;
  cash: number;
  commission: number;
  min_commission: number;
  stamp_tax: number;
  slippage: number;
  execution: 'next_open' | 'next_close';
  workers: number;
}

/** O5 单策略最优点摘要（ranking / best / per_strategy 元素） */
export interface OptimizeAllRankEntry {
  strategy: string;
  strategy_label: string;
  params: Record<string, number | string>;
  total_return: number | null;
  annual_return: number | null;
  sharpe: number | null;
  max_drawdown: number | null;
  total_trades: number;
  win_rate: number | null;
  profit_factor: number | null;
  grid_points: number;
}

/** O5 完整一键寻优结果 OptimizeAllResult */
export interface OptimizeAllResult {
  market: string;
  code: string;
  start_date: string;
  end_date: string;
  /** 按 total_return 降序 */
  ranking: OptimizeAllRankEntry[];
  best: OptimizeAllRankEntry | null;
  per_strategy: Record<string, OptimizeAllRankEntry>;
  total_strategies: number;
  total_grid_points: number;
}
