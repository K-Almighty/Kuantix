/** factor 路由 DTO（契约 §3.3） */

export interface FactorInfo {
  name: string;
  category: string;
  display_name: string | null;
  description: string;
  source: 'builtin' | 'custom';
  status: 'computed' | 'uncomputed' | 'failed';
  years: number[];
}

export interface IcPoint {
  date: string;
  ic: number;
}

export interface FactorReport {
  factor: string;
  market: string;
  start_date: string;
  end_date: string;
  sample_count: number;
  excluded_count: number;
  ic_mean: number;
  ic_std: number;
  ir: number;
  ic_positive_rate: number;
  quantile_returns: number[];
  top_minus_bottom: number;
  turnover_rate: number;
  autocorr: number;
  ic_series: IcPoint[];
}

export interface FactorModel {
  name: string;
  method: 'equal' | 'ic' | 'ir';
  weights: Record<string, number>;
  created_at: string;
}

export interface ComputeRequest {
  factors: string[];
  market: string;
  start?: string;
  end?: string;
  pool?: string;
}

export interface CombineRequest {
  factors: string[];
  method: 'equal' | 'ic' | 'ir';
  save_model: boolean;
  model_name?: string;
  market: string;
}
