/** screen 路由 DTO（契约 §3.4） */

export interface ScreenFilterInput {
  type: string;
  condition: string;
  params?: Record<string, unknown>;
}

export interface ScreenRunRequest {
  model: string;
  market: string;
  pool: string | string[];
  top_n: number;
  filters: ScreenFilterInput[];
  combine: 'and' | 'or';
  exclude_st: boolean;
  exclude_suspended: boolean;
  exclude_new: boolean;
  as_of: string | null;
}

export interface ScreenFactorRunRequest {
  factor: string;
  market: string;
  pool: string | string[];
  top_n: number;
  order: 'desc' | 'asc';
  as_of?: string | null;
  days_back?: number | null;
  filters: ScreenFilterInput[];
  combine: 'and' | 'or';
  exclude_st: boolean;
  exclude_suspended: boolean;
  exclude_new: boolean;
}

export interface ScreenBatch {
  batch_id: string;
  market: string;
  model: string;
  top_n: number;
  filters: ScreenFilterInput[];
  combine: string;
  status: 'running' | 'done' | 'failed';
  result_count: number;
  as_of: string;
  created_at: string;
  elapsed_ms: number;
}

export interface ScreenResultView {
  rank: number;
  code: string;
  name: string;
  market: string;
  score: number;
  sub_scores: Record<string, number>;
  conditions: string;
  price: number;
  as_of: string;
}

export interface FilterInfo {
  type: string;
  condition: string;
  display_name: string;
  description: string;
  params_schema: Record<string, unknown>;
}

export interface ScreenResultsQuery {
  page?: number;
  pageSize?: number;
  sortBy?: string;
  order?: 'asc' | 'desc';
  market?: string;
}
