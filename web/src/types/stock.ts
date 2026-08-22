/** 个股详情（多周期 K 线 + 技术指标，通达信风格，契约 /api/v1/stock/detail/{code}） */

/** 周期键（与后端 PERIODS 对齐） */
export type Period = 'day' | 'week' | 'month' | 'year' | 'min5' | 'min15' | 'min60';

/** 单根 K 线（OHLCV + 换手率） */
export interface StockBar {
  datetime: string;
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  /** 成交量（手/股，依数据源单位） */
  vol: number;
  /** 成交额（元） */
  amount: number;
  /** 换手率（小数，如 0.0123 = 1.23%）；无流通股本时为估算值 */
  turnover: number;
}

/** 技术指标集合（键与后端一致；缺失周期对应键不存在） */
export interface StockIndicators {
  ma5?: (number | null)[];
  ma10?: (number | null)[];
  ma20?: (number | null)[];
  ma60?: (number | null)[];
  macd?: { dif: (number | null)[]; dea: (number | null)[]; macd: (number | null)[] };
  kdj?: { k: (number | null)[]; d: (number | null)[]; j: (number | null)[] };
  rsi?: { rsi6: (number | null)[]; rsi12: (number | null)[]; rsi24: (number | null)[] };
}

/** 最新交易日行情快照（与请求周期无关，顶部报价区专用） */
export interface StockQuote {
  /** 最新交易日（YYYY-MM-DD） */
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  /** 上一交易日收盘价 */
  prev_close: number;
  /** 涨跌额（close - prev_close） */
  change: number;
  /** 涨跌幅（小数比例，如 -0.0123 = -1.23%；prev_close 为 0 时为 null） */
  change_pct: number | null;
  vol: number;
  amount: number;
  /** 换手率（小数比例）；无流通股本时为 0 */
  turnover: number;
}

/** 个股详情信封数据 */
export interface StockDetail {
  code: string;
  /** 证券名称（lake 元信息；本地无该标的时可能为空） */
  name?: string;
  market: string;
  period: Period;
  /** 该周期是否有数据（分钟线本地缺失时为 false） */
  available: boolean;
  /** 上市日期（lake 日线首根；本地无日线时为 undefined，前端按默认区间） */
  listing_date?: string;
  /** K 线数据来源：lake（本地 market.db）/ tdx_realtime（tdx 实时回退） */
  data_source?: 'lake' | 'tdx_realtime';
  /** 换手率是否为估算值（无流通股本时退化为相对量） */
  turnover_estimated: boolean;
  /** 最新交易日行情快照（任何周期都返回日口径；不可得时为 null） */
  quote?: StockQuote | null;
  bars: StockBar[];
  indicators: StockIndicators;
  /** 分钟线无数据时的提示 */
  message?: string;
}
