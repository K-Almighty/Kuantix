/** 个股详情（多周期 K 线 + 技术指标 + 实时面板，通达信风格） */

/** 周期键（与后端 PERIODS 对齐） */
export type Period =
  | 'min1'
  | 'min5'
  | 'min15'
  | 'min30'
  | 'min60'
  | 'day'
  | 'week'
  | 'month'
  | 'quarter'
  | 'year';

/** 复权方式（与后端 ADJUSTS 对齐） */
export type Adjust = 'none' | 'qfq' | 'hfq';

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

/** 技术指标集合（键与后端一致；MA 键随 ma 参数动态生成 ma{N}） */
export interface StockIndicators {
  [key: string]: unknown;
  ma5?: (number | null)[];
  ma10?: (number | null)[];
  ma20?: (number | null)[];
  ma60?: (number | null)[];
  boll?: { upper: (number | null)[]; mid: (number | null)[]; lower: (number | null)[] };
  ene?: { upper: (number | null)[]; ene: (number | null)[]; lower: (number | null)[] };
  sar?: { sar: (number | null)[] };
  macd?: { dif: (number | null)[]; dea: (number | null)[]; macd: (number | null)[] };
  kdj?: { k: (number | null)[]; d: (number | null)[]; j: (number | null)[] };
  rsi?: { rsi6: (number | null)[]; rsi12: (number | null)[]; rsi24: (number | null)[] };
  wr?: { wr6: (number | null)[]; wr10: (number | null)[] };
  bias?: { bias6: (number | null)[]; bias12: (number | null)[]; bias24: (number | null)[] };
  obv?: { obv: (number | null)[] };
  /** 分时均价线（当日累计 VWAP；min1 周期返回） */
  vwap?: (number | null)[];
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
  /** 复权方式（none/qfq/hfq） */
  adjust?: Adjust;
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

/** 五档盘口单档（价格 + 挂单量，单位手） */
export interface OrderBookLevel {
  price: number;
  vol: number;
}

/** 五档盘口 + 实时快照（/stock/order-book/{code}） */
export interface StockOrderBook {
  code: string;
  name: string;
  price: number | null;
  prev_close: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  /** 成交量（手） */
  vol: number | null;
  amount: number | null;
  /** 换手率（小数比例） */
  turnover: number | null;
  /** 量比 */
  vol_ratio: number | null;
  pe_ttm: number | null;
  pe_dynamic: number | null;
  pe_static: number | null;
  /** 每股收益 */
  eps: number | null;
  /** 每股净资产 */
  net_assets: number | null;
  /** 股息率（小数比例） */
  dividend_yield: number | null;
  /** 总市值（元） */
  total_market_cap: number | null;
  /** 总股本（股） */
  total_shares: number | null;
  /** 流通股本（股） */
  float_shares: number | null;
  /** 今日主力净流入（元） */
  main_net_amount: number | null;
  /** 买盘五档（按价格降序：买一在前） */
  bids: OrderBookLevel[];
  /** 卖盘五档（按价格升序：卖一在前） */
  asks: OrderBookLevel[];
}

/** 逐笔成交单条 */
export interface TransactionItem {
  /** HH:MM:SS */
  time: string;
  price: number;
  /** 成交量（手） */
  vol: number;
  /** 0=买（红）/ 1=卖（绿）/ 2=中性 */
  bs: 0 | 1 | 2;
}

/** 逐笔成交（/stock/transactions/{code}） */
export interface StockTransactions {
  date: string;
  items: TransactionItem[];
}

/** 资金流向（/stock/capital-flow/{code}，单位元） */
export interface StockCapitalFlow {
  main_in: number;
  main_out: number;
  main_net: number;
  small_in: number;
  small_out: number;
  small_net: number;
  mid_net_5d: number;
  large_net_5d: number;
}

/** 轻量实时报价（自选股侧栏，/stock/quotes） */
export interface StockQuoteLite {
  code: string;
  price: number;
  prev_close: number;
  change: number;
  /** 涨跌幅（小数比例） */
  change_pct: number | null;
  vol: number;
  amount: number;
}

/** 画线工具（K 线图表交互：趋势线/水平线/矩形/黄金分割/文本） */
export type DrawTool = 'none' | 'trend' | 'hline' | 'rect' | 'fib' | 'text';

/** 主图叠加指标（单选；EXPMA 前端计算，其余来自后端 indicators） */
export type MainIndicator = 'none' | 'ma' | 'expma' | 'boll' | 'ene' | 'sar';
