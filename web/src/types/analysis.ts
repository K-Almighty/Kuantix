/**
 * 盘前分析 / 盘后复盘（Analysis）类型定义。
 * 与后端 Kuantix.core.contracts 保持字段一一对应。
 */

/* ---------------- 枚举 ---------------- */

export type NewsCategory = 'news' | 'announcement' | 'policy';
export type FundamentalGrade = 'A' | 'B' | 'C' | 'D';
export type TrendDirection = 'up' | 'down' | 'flat';
/**
 * 涨停板类型（中文枚举值，与后端 LimitType.value 一致）
 */
export type LimitType =
  | '业绩驱动'
  | '概念炒作'
  | '技术突破'
  | '新股上市'
  | 'ST摘帽'
  | '其他';

/* ---------------- 核心 DTO ---------------- */

export interface NewsItem {
  id: string;
  source: string;
  category: NewsCategory;
  title: string;
  url: string;
  /** ISO 秒级时间戳 */
  publish_ts: string;
  codes: string[];
  /** 0–10，越高越重要 */
  importance: number;
  matched_keywords: string[];
  summary: string;
}

export interface FundamentalProfile {
  code: string;
  name: string;
  market: string;
  sector: string;
  industry: string;
  market_cap: number;
  pe: number | null;
  pb: number | null;
  roe: number | null;
  revenue_growth: number | null;
  net_profit_growth: number | null;
  debt_ratio: number | null;
  dividend_yield: number | null;
  latest_announcements: string[];
  grade: FundamentalGrade;
  summary_lines: string[];
}

export interface TechnicalAnalysis {
  code: string;
  /** 证券名称（lake 元信息；缺失时前端兜底显示代码） */
  name: string;
  /** K 线数据来源：lake（本地）/ tdx_realtime（tdx 实时补全） */
  data_source?: 'lake' | 'tdx_realtime';
  last_date: string;
  ma5: number | null;
  ma10: number | null;
  ma20: number | null;
  ma60: number | null;
  ma120: number | null;
  ma250: number | null;
  macd_dif_last: number | null;
  macd_dea_last: number | null;
  macd_hist_last: number | null;
  rsi_last: number | null;
  kdj_k_last: number | null;
  kdj_d_last: number | null;
  kdj_j_last: number | null;
  boll_upper_last: number | null;
  boll_mid_last: number | null;
  boll_lower_last: number | null;
  trend_direction: TrendDirection;
  /** 0–1 */
  trend_strength: number;
  support_levels: number[];
  resistance_levels: number[];
  signals: string[];
}

export interface LimitEntry {
  code: string;
  name: string;
  sector: string;
  limit_type: LimitType;
  close: number;
  /** 比例，0.098 = 9.8% */
  change_pct: number;
  volume_ratio: number | null;
  continuous_days: number;
  reasons: string[];
  is_up: boolean;
}

export interface LimitSectorStat {
  sector: string;
  up: number;
  down: number;
}

export interface LimitTypeStat {
  limit_type: LimitType | string;
  count: number;
}

export interface LimitUpDownSummary {
  date: string;
  market: string;
  up_count: number;
  down_count: number;
  flat_count: number;
  total_count: number;
  up_ratio: number;
  down_ratio: number;
  by_sector: LimitSectorStat[];
  by_type: LimitTypeStat[];
  generated_at: string;
}

/* ---------------- 报告壳 ---------------- */

export interface PreOpenReport {
  date: string;
  market: string;
  generated_at: string;
  news_feed_summary: {
    total: number;
    by_category: Array<{ category: NewsCategory; count: number }>;
    top_news: NewsItem[];
  };
  watchlist_profiles: FundamentalProfile[];
  broad_market_scan_top: TechnicalAnalysis[];
}

export interface PostCloseReport {
  date: string;
  market: string;
  generated_at: string;
  /** 数据来源：lake（本地）/ tdx_realtime（tdx 实时补全当天）/ lake_fallback（降级到最近可得日） */
  data_source: 'lake' | 'tdx_realtime' | 'lake_fallback';
  /** 降级展示时标记数据实际截至日 */
  data_as_of: string | null;
  limit_summary: LimitUpDownSummary | null;
  tech_highlights: TechnicalAnalysis[];
  signals_today: Array<{
    code: string;
    name?: string;
    /** 信号列表（后端字段为 signals 数组） */
    signals: string[];
    date: string;
    trend_direction?: TrendDirection;
    trend_strength?: number;
    data_source?: string;
    [k: string]: unknown;
  }>;
  watchlist_pnl: Array<{ code: string; name: string; last: number; cost: number | null; pnl: number | null; pnl_pct: number | null; [k: string]: unknown }>;
}

/* ---------------- 组合响应 ---------------- */

export interface LimitUpDownResponse {
  summary: LimitUpDownSummary | null;
  entries: import('./envelope').Page<LimitEntry>;
}
