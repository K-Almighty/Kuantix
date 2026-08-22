/**
 * API 数据层抽象：KuantixApi 接口 + 共享类型。
 * 契约锁定文档 api-contract.md；字段名/端点路径严格照抄，禁止自创。
 */
import type { Envelope, Page } from '../types/envelope';
import type { VersionInfo, HealthInfo } from '../types/infra';
import type {
  DataLakeStatus,
  Job,
  SyncRequest,
  VerifyReport,
  QuarantineEntry,
  VerifyQuarantineRemoveResult,
  SecuritySearchResult,
} from '../types/data';
import type {
  FactorInfo,
  FactorReport,
  FactorModel,
  ComputeRequest,
  CombineRequest,
} from '../types/factor';
import type {
  FilterInfo,
  ScreenBatch,
  ScreenResultView,
  ScreenRunRequest,
  ScreenFactorRunRequest,
  ScreenResultsQuery,
} from '../types/screen';
import type {
  MonitorStatus,
  WatchlistItem,
  WatchlistAddResult,
  RemoveResult,
  Rule,
  RuleInput,
  CriterionInfo,
  PositionView,
  PositionInput,
  Alert,
  AlertLevel,
  ChannelInfo,
  WsEnvelope,
} from '../types/monitor';
import type {
  BacktestStrategySchema,
  BacktestRunRequest,
  BacktestResult,
} from '../types/backtest';
import type {
  PortfolioRunRequest,
  PortfolioResult,
} from '../types/portfolio';
import type {
  MultiStrategyRunRequest,
  SavedStrategy,
  SavedStrategyCreate,
  StrategyKind,
} from '../types/strategy';
import type {
  OptimizeRunRequest,
  OptimizeResult,
  OptimizeAllRunRequest,
  OptimizeAllResult,
} from '../types/optimize';
import type {
  SettingsStatus,
  TestConnectionRequest,
  TestConnectionResult,
} from '../types/settings';
import type { BacktestJobList, KlineWithSignals } from '../types/backtest';
import type { StockDetail } from '../types/stock';
import type {
  NewsItem,
  FundamentalProfile,
  TechnicalAnalysis,
  PreOpenReport,
  PostCloseReport,
  LimitUpDownResponse,
  NewsCategory,
  FundamentalGrade,
} from '../types/analysis';

/** 导出产物（JSON 信封文本 / CSV 文件），前端 Blob 下载（契约 §1.10） */
export interface ExportPayload {
  blob: Blob;
  filename: string;
}

export type WsConnectionStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

/** WS 订阅者接口（M17 协议，契约 §2.4.1） */
export interface MonitorFeed {
  connect(): void;
  close(): void;
  sendPing(): void;
  onMessage(handler: (frame: WsEnvelope) => void): void;
  onStatusChange(handler: (status: WsConnectionStatus, detail?: string) => void): void;
}

/** API 错误（code≠0 或网络/解析失败），附带契约错误码 */
export class ApiError extends Error {
  readonly code: number;
  readonly detail: unknown;

  constructor(code: number, message: string, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.detail = detail;
  }
}

/** 前端可消费的全部端点（业务 36 端点 + 基础设施 4 端点） */
export interface KuantixApi {
  /* -------- 基础设施（§2.0） -------- */
  getVersion(): Promise<Envelope<VersionInfo>>;
  getHealth(): Promise<Envelope<HealthInfo>>;

  /* -------- data（D1–D8） -------- */
  getDataStatus(market?: string): Promise<Envelope<DataLakeStatus>>;
  postDataSync(req: SyncRequest): Promise<Envelope<Job>>;
  getDataSyncJob(jobId: string): Promise<Envelope<Job>>;
  postDataSyncCancel(jobId: string): Promise<Envelope<Job>>;
  getDataVerify(market?: string): Promise<Envelope<VerifyReport>>;
  getDataQuarantine(market?: string, page?: number, pageSize?: number): Promise<Envelope<Page<QuarantineEntry>>>;
  deleteDataQuarantine(code: string, market?: string): Promise<Envelope<VerifyQuarantineRemoveResult>>;
  /** D8 证券搜索（v1.2 增量）：q 支持代码（精确/前缀）或名称（模糊） */
  searchSecurities(q: string, market?: string, limit?: number): Promise<Envelope<SecuritySearchResult>>;

  /* -------- factor（F1–F6） -------- */
  getFactors(market?: string, page?: number, pageSize?: number): Promise<Envelope<Page<FactorInfo>>>;
  postFactorCompute(req: ComputeRequest): Promise<Envelope<Job>>;
  getFactorJob(jobId: string): Promise<Envelope<Job>>;
  postFactorReport(
    req: { name: string; market?: string; start?: string; end?: string },
  ): Promise<Envelope<Job>>;
  postFactorCombine(req: CombineRequest): Promise<Envelope<FactorModel>>;
  getFactorModels(market?: string, page?: number, pageSize?: number): Promise<Envelope<Page<FactorModel>>>;

  /* -------- screen（S1–S6） -------- */
  getScreenFilters(market?: string): Promise<Envelope<{ items: FilterInfo[] }>>;
  postScreenRun(req: ScreenRunRequest): Promise<Envelope<Job>>;
  screenFactorRun(req: ScreenFactorRunRequest): Promise<Envelope<Page<ScreenResultView>>>;
  getScreenJob(jobId: string): Promise<Envelope<Job>>;
  getScreenBatches(market?: string, page?: number, pageSize?: number): Promise<Envelope<Page<ScreenBatch>>>;
  getScreenResults(
    batchId: string,
    opts?: ScreenResultsQuery,
  ): Promise<Envelope<Page<ScreenResultView>>>;
  exportScreenResults(batchId: string, format: 'json' | 'csv', market?: string): Promise<ExportPayload>;

  /* -------- monitor（M1–M17） -------- */
  postMonitorStart(market?: string): Promise<Envelope<MonitorStatus>>;
  postMonitorStop(market?: string): Promise<Envelope<MonitorStatus>>;
  getMonitorStatus(market?: string): Promise<Envelope<MonitorStatus>>;
  getWatchlist(market?: string, page?: number, pageSize?: number): Promise<Envelope<Page<WatchlistItem>>>;
  postWatchlist(codes: string[], market?: string, source?: string): Promise<Envelope<WatchlistAddResult>>;
  deleteWatchlist(code: string, market?: string): Promise<Envelope<RemoveResult>>;
  getCriteria(): Promise<Envelope<{ items: CriterionInfo[] }>>;
  getRules(market?: string, page?: number, pageSize?: number): Promise<Envelope<Page<Rule>>>;
  postRule(input: RuleInput): Promise<Envelope<Rule>>;
  putRule(id: string, input: RuleInput): Promise<Envelope<Rule>>;
  deleteRule(id: string): Promise<Envelope<RemoveResult>>;
  getPositions(market?: string, page?: number, pageSize?: number): Promise<Envelope<Page<PositionView>>>;
  postPosition(input: PositionInput): Promise<Envelope<PositionView>>;
  deletePosition(code: string, market?: string): Promise<Envelope<RemoveResult>>;
  getAlerts(opts?: {
    market?: string;
    level?: AlertLevel;
    page?: number;
    pageSize?: number;
  }): Promise<Envelope<Page<Alert>>>;
  getChannels(): Promise<Envelope<{ items: ChannelInfo[] }>>;
  connectMonitorFeed(market?: string): MonitorFeed;

  /* -------- backtest（B1–B4，v1.2 增量） -------- */
  getBacktestStrategies(): Promise<Envelope<{ items: BacktestStrategySchema[]; count: number }>>;
  postBacktestRun(req: BacktestRunRequest): Promise<Envelope<Job>>;
  getBacktestJob(jobId: string): Promise<Envelope<Job>>;
  getBacktestResult(jobId: string): Promise<Envelope<BacktestResult>>;

  /* -------- optimize（O1–O3，v1.3 草案 P1：单策略参数网格寻优） -------- */
  /** O1 触发寻优 → Job（module=backtest, action=optimize） */
  optimizeRun(req: OptimizeRunRequest): Promise<Envelope<Job>>;
  /** O2 寻优进度（同 B3 模式） */
  optimizeJob(jobId: string): Promise<Envelope<Job>>;
  /** O3 寻优完整结果（results/best/heatmap） */
  optimizeResult(jobId: string): Promise<Envelope<OptimizeResult>>;
  /** O6 删除单个策略寻优（job + 结果），返回 {job_id,deleted_job,deleted_result} */
  deleteOptimizeJob(jobId: string): Promise<Envelope<{ job_id: string; deleted_job: boolean; deleted_result: boolean }>>;
  /** O4 一键寻优所有策略 → Job */
  optimizeAllRun(req: OptimizeAllRunRequest): Promise<Envelope<Job>>;
  /** O5 一键寻优所有策略完整结果（ranking/best/per_strategy） */
  optimizeAllResult(jobId: string): Promise<Envelope<OptimizeAllResult>>;

  /* -------- compare（C1，v1.3 草案 P1：回测任务列表） -------- */
  /** C1 任务列表：{items: [Job], count}；limit 1..50 默认 20；status 可空默认全部 */
  getBacktestJobs(opts?: { limit?: number; status?: string; module?: string }): Promise<Envelope<BacktestJobList>>;

  /* -------- K 线下钻（P1，B5：单标的 K线 + 买卖点信号标注） -------- */
  /** B5 K 线 + 买卖点：query 按契约 §3.8（market/start/end/strategy 均可空，后端有默认值） */
  getKline(
    code: string,
    opts?: { market?: string; start?: string; end?: string; strategy?: string },
  ): Promise<Envelope<KlineWithSignals>>;

  /* -------- stock（个股详情：多周期 K 线 + 技术指标 + 核心数据，通达信风格） -------- */
  /** 个股详情：period ∈ day|week|month|year|min5|min15；indicators 逗号分隔 ma,macd,kdj,rsi */
  getStockDetail(
    code: string,
    opts?: { market?: string; period?: string; limit?: number; indicators?: string },
  ): Promise<Envelope<StockDetail>>;

  /* -------- portfolio（P1–P3，v1.3 草案 P0：资金分仓组合回测） -------- */
  portfolioRun(req: PortfolioRunRequest): Promise<Envelope<Job>>;
  getPortfolioJob(jobId: string): Promise<Envelope<Job>>;
  getPortfolioResult(jobId: string): Promise<Envelope<PortfolioResult>>;

  /* -------- strategies（S1–S5，v1.3 草案 P0：策略库 CRUD + 多策略组合回测） -------- */
  getStrategies(opts?: {
    kind?: StrategyKind | '';
    page?: number;
    pageSize?: number;
  }): Promise<Envelope<Page<SavedStrategy>>>;
  createStrategy(req: SavedStrategyCreate): Promise<Envelope<SavedStrategy>>;
  getStrategy(strategyId: string): Promise<Envelope<SavedStrategy>>;
  deleteStrategy(strategyId: string): Promise<Envelope<RemoveResult>>;
  strategiesRunMulti(req: MultiStrategyRunRequest): Promise<Envelope<Job>>;

  /* -------- settings（E1–E2，v1.3 增量 P2：**只读**数据源状态，NF-20） -------- */
  /** E1 只读数据源状态：config 摘要 + known_hosts（只读）+ 数据湖摘要 + 版本 */
  getSettingsStatus(): Promise<Envelope<SettingsStatus>>;
  /** E2 主机连通性测试（只测不写；连接失败返回 {ok:false} 业务结果，非 HTTP 错误） */
  testConnection(req: TestConnectionRequest): Promise<Envelope<TestConnectionResult>>;

  /* -------- analysis（盘前 / 盘后，P1：Pre/Post 报告 + 列表 + 导出） -------- */
  getPreOpenReport(opts?: {
    market?: string;
    date?: string;
    codes?: string;
  }): Promise<Envelope<PreOpenReport>>;
  postPreOpenRun(opts?: { market?: string; date?: string }): Promise<Envelope<PreOpenReport>>;
  getPreOpenNews(opts?: {
    market?: string;
    date?: string;
    category?: NewsCategory | '';
    keywords?: string[];
    page?: number;
    pageSize?: number;
  }): Promise<Envelope<Page<NewsItem>>>;
  getPreOpenFundamentals(opts?: {
    market?: string;
    date?: string;
    codes?: string;
    grade?: FundamentalGrade | '';
    page?: number;
    pageSize?: number;
  }): Promise<Envelope<Page<FundamentalProfile>>>;
  postPreOpenFundamentalsRun(opts: {
    codes: string;
    market?: string;
    date?: string;
  }): Promise<Envelope<{ count: number; items: FundamentalProfile[] }>>;
  getPostCloseReport(opts?: {
    market?: string;
    date?: string;
    codes?: string;
  }): Promise<Envelope<PostCloseReport>>;
  postPostCloseRun(opts?: {
    market?: string;
    date?: string;
    force?: boolean;
  }): Promise<Envelope<PostCloseReport>>;
  getPostCloseLimit(opts?: {
    market?: string;
    date?: string;
    limit_type?: string;
    sector?: string;
    only_up?: 'true' | 'false' | '';
    page?: number;
    pageSize?: number;
  }): Promise<Envelope<LimitUpDownResponse>>;
  getPostCloseTechnical(opts?: {
    market?: string;
    codes?: string;
    page?: number;
    pageSize?: number;
  }): Promise<Envelope<Page<TechnicalAnalysis>>>;
  exportPreOpenReport(
    format: 'md' | 'json',
    opts?: { market?: string; date?: string },
  ): Promise<ExportPayload>;
  exportPostCloseReport(
    format: 'md' | 'json',
    opts?: { market?: string; date?: string },
  ): Promise<ExportPayload>;
}
