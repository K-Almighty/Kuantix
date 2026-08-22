/**
 * REST 客户端（真实后端模式）。
 * 统一信封解析（NF-9）：code≠0 抛 ApiError；交互类请求自动 toast；轮询类静默（由 store 呈现错误态）。
 * Base URL 从 VITE_API_BASE 读取（默认 http://127.0.0.1:8899/api/v1），禁止硬编码端口（契约 §1.1/§8）。
 */
import type { Envelope, Meta, Page } from '../types/envelope';
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
} from '../types/monitor';
import type {
  BacktestStrategySchema,
  BacktestRunRequest,
  BacktestResult,
  BacktestJobList,
  KlineWithSignals,
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
import type { StockDetail } from '../types/stock';
import type { ExportPayload, MonitorFeed, KuantixApi } from './types';
import { ApiError } from './types';
import { RealMonitorFeed } from './ws';
import { API_BASE, serverOrigin } from './config';
import { toastError } from '../utils/toast';

function emptyMeta(): Meta {
  return {
    generated_at: new Date().toISOString(),
    data_date: null,
    market: 'CN',
    elapsed_ms: 0,
    version: '0.1.0',
  };
}

/** 组装人类可读错误信息（含 fail-loud 明细） */
function describeError(env: Envelope<unknown>): string {
  const parts: string[] = [`[${env.code}] ${env.message || '未知错误'}`];
  const d = env.data;
  if (d && typeof d === 'object') {
    const obj = d as Record<string, unknown>;
    if (typeof obj.error_type === 'string') parts.push(`类型:${obj.error_type}`);
    if (typeof obj.path === 'string') parts.push(`路径:${obj.path}`);
    if (Array.isArray(obj.errors) && obj.errors.length > 0) parts.push(`明细:${JSON.stringify(obj.errors)}`);
  }
  return parts.join(' · ');
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  /** 交互类请求 code≠0 时弹错误 toast；轮询类传 false */
  toastOnError?: boolean;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<Envelope<T>> {
  const { method = 'GET', body, query, toastOnError = true } = opts;
  const target = path.startsWith('http') ? path : API_BASE + path;
  const url = new URL(target, window.location.origin);
  if (query) {
    Object.entries(query).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
    });
  }
  let resp: Response;
  try {
    resp = await fetch(url.toString(), {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    const msg = `网络错误：无法连接 ${API_BASE}（${(e as Error).message || 'fetch 失败'}）`;
    if (toastOnError) toastError(msg);
    throw new ApiError(-1, msg);
  }
  let env: Envelope<T>;
  try {
    env = (await resp.json()) as Envelope<T>;
  } catch {
    const msg = `HTTP ${resp.status}：响应不是 JSON（契约 §1.2 要求统一信封）`;
    if (toastOnError) toastError(msg);
    throw new ApiError(resp.status, msg);
  }
  if (env.code !== 0) {
    const msg = describeError(env);
    if (toastOnError) toastError(msg);
    throw new ApiError(env.code, msg, env.data);
  }
  return env;
}

export class RestApi implements KuantixApi {
  /* -------- 基础设施（§2.0） -------- */
  getVersion(): Promise<Envelope<VersionInfo>> {
    return request<VersionInfo>(`${serverOrigin()}/api/version`, { toastOnError: false });
  }

  getHealth(): Promise<Envelope<HealthInfo>> {
    return request<HealthInfo>(`${serverOrigin()}/health`, { toastOnError: false });
  }

  /* -------- data（D1–D7） -------- */
  getDataStatus(market = 'CN'): Promise<Envelope<DataLakeStatus>> {
    return request<DataLakeStatus>('/data/status', { query: { market }, toastOnError: false });
  }

  postDataSync(req: SyncRequest): Promise<Envelope<Job>> {
    return request<Job>('/data/sync', { method: 'POST', body: req });
  }

  getDataSyncJob(jobId: string): Promise<Envelope<Job>> {
    return request<Job>(`/data/sync/${jobId}`, { toastOnError: false });
  }

  postDataSyncCancel(jobId: string): Promise<Envelope<Job>> {
    return request<Job>(`/data/sync/${jobId}/cancel`, { method: 'POST' });
  }

  getDataVerify(market = 'CN'): Promise<Envelope<VerifyReport>> {
    return request<VerifyReport>('/data/verify', { query: { market } });
  }

  getDataQuarantine(market = 'CN', page = 1, pageSize = 50): Promise<Envelope<Page<QuarantineEntry>>> {
    return request<Page<QuarantineEntry>>('/data/quarantine', { query: { market, page, page_size: pageSize } });
  }

  deleteDataQuarantine(code: string, market = 'CN'): Promise<Envelope<VerifyQuarantineRemoveResult>> {
    return request<VerifyQuarantineRemoveResult>(`/data/quarantine/${code}`, {
      method: 'DELETE',
      query: { market },
    });
  }

  searchSecurities(q: string, market = 'CN', limit = 20): Promise<Envelope<SecuritySearchResult>> {
    return request<SecuritySearchResult>('/data/search', { query: { q, market, limit } });
  }

  /* -------- factor（F1–F6） -------- */
  getFactors(market = 'CN', page = 1, pageSize = 500): Promise<Envelope<Page<FactorInfo>>> {
    return request<Page<FactorInfo>>('/factor', { query: { market, page, page_size: pageSize } });
  }

  postFactorCompute(req: ComputeRequest): Promise<Envelope<Job>> {
    return request<Job>('/factor/compute', { method: 'POST', body: req });
  }

  getFactorJob(jobId: string): Promise<Envelope<Job>> {
    return request<Job>(`/factor/jobs/${jobId}`, { toastOnError: false });
  }

  postFactorReport(
    req: { name: string; market?: string; start?: string; end?: string },
  ): Promise<Envelope<Job>> {
    return request<Job>('/factor/report', { method: 'POST', body: req });
  }

  postFactorCombine(req: CombineRequest): Promise<Envelope<Job>> {
    return request<Job>('/factor/combine', { method: 'POST', body: req });
  }

  getFactorModels(market = 'CN', page = 1, pageSize = 100): Promise<Envelope<Page<FactorModel>>> {
    return request<Page<FactorModel>>('/factor/models', { query: { market, page, page_size: pageSize } });
  }

  /* -------- screen（S1–S6） -------- */
  getScreenFilters(market = 'CN'): Promise<Envelope<{ items: FilterInfo[] }>> {
    return request<{ items: FilterInfo[] }>('/screen/filters', { query: { market } });
  }

  postScreenRun(req: ScreenRunRequest): Promise<Envelope<Job>> {
    return request<Job>('/screen/run', { method: 'POST', body: req });
  }

  screenFactorRun(req: ScreenFactorRunRequest): Promise<Envelope<Page<ScreenResultView>>> {
    return request<Page<ScreenResultView>>('/screen/factor-run', { method: 'POST', body: req });
  }

  getScreenJob(jobId: string): Promise<Envelope<Job>> {
    return request<Job>(`/screen/jobs/${jobId}`, { toastOnError: false });
  }

  getScreenBatches(market = 'CN', page = 1, pageSize = 20): Promise<Envelope<Page<ScreenBatch>>> {
    return request<Page<ScreenBatch>>('/screen/batches', { query: { market, page, page_size: pageSize } });
  }

  getScreenResults(batchId: string, opts: ScreenResultsQuery = {}): Promise<Envelope<Page<ScreenResultView>>> {
    return request<Page<ScreenResultView>>('/screen/results', {
      query: {
        batch_id: batchId,
        page: opts.page ?? 1,
        page_size: opts.pageSize ?? 50,
        sort_by: opts.sortBy ?? 'score',
        order: opts.order ?? 'desc',
        market: opts.market ?? 'CN',
      },
    });
  }

  async exportScreenResults(batchId: string, format: 'json' | 'csv' = 'json', market = 'CN'): Promise<ExportPayload> {
    const url = `${API_BASE}/screen/results/${encodeURIComponent(batchId)}/export?format=${encodeURIComponent(
      format,
    )}&market=${encodeURIComponent(market)}`;
    let resp: Response;
    try {
      resp = await fetch(url);
    } catch {
      const msg = '导出失败：无法连接后端';
      toastError(msg);
      throw new ApiError(-1, msg);
    }
    if (!resp.ok) {
      let env: Envelope<unknown>;
      try {
        env = (await resp.json()) as Envelope<unknown>;
      } catch {
        const msg = `导出失败：HTTP ${resp.status}`;
        toastError(msg);
        throw new ApiError(resp.status, msg);
      }
      const msg = describeError(env);
      toastError(msg);
      throw new ApiError(env.code, msg, env.data);
    }
    const blob = await resp.blob();
    const disposition = resp.headers.get('content-disposition') ?? '';
    const m = /filename="?([^";]+)"?/.exec(disposition);
    const filename = m?.[1] ?? `screen_${batchId}.${format === 'csv' ? 'csv' : 'json'}`;
    return { blob, filename };
  }

  /* -------- monitor（M1–M17） -------- */
  postMonitorStart(market = 'CN'): Promise<Envelope<MonitorStatus>> {
    return request<MonitorStatus>('/monitor/start', { method: 'POST', query: { market } });
  }

  postMonitorStop(market = 'CN'): Promise<Envelope<MonitorStatus>> {
    return request<MonitorStatus>('/monitor/stop', { method: 'POST', query: { market } });
  }

  getMonitorStatus(market = 'CN'): Promise<Envelope<MonitorStatus>> {
    return request<MonitorStatus>('/monitor/status', { query: { market }, toastOnError: false });
  }

  getWatchlist(market = 'CN', page = 1, pageSize = 50): Promise<Envelope<Page<WatchlistItem>>> {
    return request<Page<WatchlistItem>>('/monitor/watchlist', {
      query: { market, page, page_size: pageSize },
      toastOnError: false,
    });
  }

  postWatchlist(codes: string[], market = 'CN', source = 'manual'): Promise<Envelope<WatchlistAddResult>> {
    return request<WatchlistAddResult>('/monitor/watchlist', { method: 'POST', body: { market, codes, source } });
  }

  deleteWatchlist(code: string, market = 'CN'): Promise<Envelope<RemoveResult>> {
    return request<RemoveResult>(`/monitor/watchlist/${code}`, { method: 'DELETE', query: { market } });
  }

  getCriteria(): Promise<Envelope<{ items: CriterionInfo[] }>> {
    return request<{ items: CriterionInfo[] }>('/monitor/criteria');
  }

  getRules(market = 'CN', page = 1, pageSize = 50): Promise<Envelope<Page<Rule>>> {
    return request<Page<Rule>>('/monitor/rules', {
      query: { market, page, page_size: pageSize },
      toastOnError: false,
    });
  }

  postRule(input: RuleInput): Promise<Envelope<Rule>> {
    return request<Rule>('/monitor/rules', { method: 'POST', body: input });
  }

  putRule(id: string, input: RuleInput): Promise<Envelope<Rule>> {
    return request<Rule>(`/monitor/rules/${id}`, { method: 'PUT', body: input });
  }

  deleteRule(id: string): Promise<Envelope<RemoveResult>> {
    return request<RemoveResult>(`/monitor/rules/${id}`, { method: 'DELETE' });
  }

  getPositions(market = 'CN', page = 1, pageSize = 50): Promise<Envelope<Page<PositionView>>> {
    return request<Page<PositionView>>('/monitor/positions', {
      query: { market, page, page_size: pageSize },
      toastOnError: false,
    });
  }

  postPosition(input: PositionInput): Promise<Envelope<PositionView>> {
    return request<PositionView>('/monitor/positions', { method: 'POST', body: input });
  }

  deletePosition(code: string, market = 'CN'): Promise<Envelope<RemoveResult>> {
    return request<RemoveResult>(`/monitor/positions/${code}`, { method: 'DELETE', query: { market } });
  }

  getAlerts(opts: { market?: string; level?: AlertLevel; page?: number; pageSize?: number } = {}): Promise<
    Envelope<Page<Alert>>
  > {
    return request<Page<Alert>>('/monitor/alerts', {
      query: {
        market: opts.market ?? 'CN',
        level: opts.level,
        page: opts.page ?? 1,
        page_size: opts.pageSize ?? 50,
      },
      toastOnError: false,
    });
  }

  getChannels(): Promise<Envelope<{ items: ChannelInfo[] }>> {
    return request<{ items: ChannelInfo[] }>('/monitor/channels');
  }

  getPresets(): Promise<Envelope<PresetStatus[]>> {
    return request<PresetStatus[]>('/monitor/presets', { toastOnError: false });
  }

  applyPreset(key: string): Promise<Envelope<Rule>> {
    return request<Rule>(`/monitor/presets/${key}`, { method: 'POST' });
  }

  togglePreset(key: string): Promise<Envelope<Rule>> {
    return request<Rule>(`/monitor/presets/${key}/toggle`, { method: 'POST' });
  }

  connectMonitorFeed(market = 'CN'): MonitorFeed {
    return new RealMonitorFeed(market);
  }

  /* -------- backtest（B1–B4，v1.2 增量） -------- */
  getBacktestStrategies(): Promise<Envelope<{ items: BacktestStrategySchema[]; count: number }>> {
    return request<{ items: BacktestStrategySchema[]; count: number }>('/backtest/strategies');
  }

  postBacktestRun(req: BacktestRunRequest): Promise<Envelope<Job>> {
    return request<Job>('/backtest/run', { method: 'POST', body: req });
  }

  getBacktestJob(jobId: string): Promise<Envelope<Job>> {
    return request<Job>(`/backtest/jobs/${jobId}`, { toastOnError: false });
  }

  getBacktestResult(jobId: string): Promise<Envelope<BacktestResult>> {
    return request<BacktestResult>(`/backtest/results/${jobId}`, { toastOnError: false });
  }

  /* -------- portfolio（P1–P3，v1.3 草案 P0） -------- */
  portfolioRun(req: PortfolioRunRequest): Promise<Envelope<Job>> {
    return request<Job>('/portfolio/run', { method: 'POST', body: req });
  }

  getPortfolioJob(jobId: string): Promise<Envelope<Job>> {
    return request<Job>(`/portfolio/jobs/${jobId}`, { toastOnError: false });
  }

  getPortfolioResult(jobId: string): Promise<Envelope<PortfolioResult>> {
    return request<PortfolioResult>(`/portfolio/results/${jobId}`, { toastOnError: false });
  }

  /* -------- strategies（S1–S5，v1.3 草案 P0） -------- */
  getStrategies(opts: { kind?: StrategyKind | ''; page?: number; pageSize?: number } = {}): Promise<
    Envelope<Page<SavedStrategy>>
  > {
    return request<Page<SavedStrategy>>('/strategies', {
      query: { kind: opts.kind, page: opts.page ?? 1, page_size: opts.pageSize ?? 20 },
      toastOnError: false,
    });
  }

  createStrategy(req: SavedStrategyCreate): Promise<Envelope<SavedStrategy>> {
    return request<SavedStrategy>('/strategies', { method: 'POST', body: req });
  }

  getStrategy(strategyId: string): Promise<Envelope<SavedStrategy>> {
    return request<SavedStrategy>(`/strategies/${strategyId}`, { toastOnError: false });
  }

  deleteStrategy(strategyId: string): Promise<Envelope<RemoveResult>> {
    return request<RemoveResult>(`/strategies/${strategyId}`, { method: 'DELETE' });
  }

  strategiesRunMulti(req: MultiStrategyRunRequest): Promise<Envelope<Job>> {
    return request<Job>('/strategies/run-multi', { method: 'POST', body: req });
  }

  /* -------- optimize（O1–O3，v1.3 草案 P1） -------- */
  optimizeRun(req: OptimizeRunRequest): Promise<Envelope<Job>> {
    return request<Job>('/optimize/run', { method: 'POST', body: req });
  }

  optimizeJob(jobId: string): Promise<Envelope<Job>> {
    return request<Job>(`/optimize/jobs/${jobId}`, { toastOnError: false });
  }

  optimizeResult(jobId: string): Promise<Envelope<OptimizeResult>> {
    return request<OptimizeResult>(`/optimize/results/${jobId}`, { toastOnError: false });
  }

  deleteOptimizeJob(jobId: string): Promise<Envelope<{ job_id: string; deleted_job: boolean; deleted_result: boolean }>> {
    return request<{ job_id: string; deleted_job: boolean; deleted_result: boolean }>(`/optimize/jobs/${jobId}`, {
      method: 'DELETE',
    });
  }

  optimizeAllRun(req: OptimizeAllRunRequest): Promise<Envelope<Job>> {
    return request<Job>('/optimize/all/run', { method: 'POST', body: req });
  }

  optimizeAllResult(jobId: string): Promise<Envelope<OptimizeAllResult>> {
    return request<OptimizeAllResult>(`/optimize/all/results/${jobId}`, { toastOnError: false });
  }

  /* -------- compare（C1，v1.3 草案 P1） -------- */
  getBacktestJobs(opts: { limit?: number; status?: string; module?: string } = {}): Promise<
    Envelope<BacktestJobList>
  > {
    return request<BacktestJobList>('/backtest/jobs', {
      query: {
        limit: opts.limit ?? 20,
        status: opts.status,
        module: opts.module ?? 'backtest',
      },
      toastOnError: false,
    });
  }

  /* -------- K 线下钻（P1，B5，契约 §3.8） -------- */
  getKline(
    code: string,
    opts: { market?: string; start?: string; end?: string; strategy?: string } = {},
  ): Promise<Envelope<KlineWithSignals>> {
    return request<KlineWithSignals>(`/backtest/kline/${encodeURIComponent(code)}`, {
      query: { market: opts.market, start: opts.start, end: opts.end, strategy: opts.strategy },
      toastOnError: false,
    });
  }

  /* -------- stock（个股详情：多周期 K 线 + 技术指标，通达信风格） -------- */
  getStockDetail(
    code: string,
    opts: { market?: string; period?: string; limit?: number; indicators?: string } = {},
  ): Promise<Envelope<StockDetail>> {
    return request<StockDetail>(`/stock/detail/${encodeURIComponent(code)}`, {
      query: {
        market: opts.market ?? 'CN',
        period: opts.period ?? 'day',
        limit: opts.limit,
        indicators: opts.indicators,
      },
      toastOnError: false,
    });
  }

  /* -------- settings（E1–E2，v1.3 增量 P2：只读数据源状态，NF-20） -------- */
  getSettingsStatus(): Promise<Envelope<SettingsStatus>> {
    return request<SettingsStatus>('/settings/status', { toastOnError: false });
  }

  testConnection(req: TestConnectionRequest): Promise<Envelope<TestConnectionResult>> {
    return request<TestConnectionResult>('/settings/test-connection', { method: 'POST', body: req });
  }

  /* -------- analysis（盘前 / 盘后） -------- */

  getPreOpenReport(opts?: {
    market?: string;
    date?: string;
    codes?: string;
  }): Promise<Envelope<PreOpenReport>> {
    return request<PreOpenReport>('/analysis/pre-open/report', {
      query: {
        market: opts?.market ?? 'CN',
        date: opts?.date,
        codes: opts?.codes,
      },
      toastOnError: false,
    });
  }

  postPreOpenRun(opts?: { market?: string; date?: string }): Promise<Envelope<PreOpenReport>> {
    return request<PreOpenReport>('/analysis/pre-open/run', {
      method: 'POST',
      query: { market: opts?.market ?? 'CN', date: opts?.date },
    });
  }

  getPreOpenNews(opts?: {
    market?: string;
    date?: string;
    category?: NewsCategory | '';
    keywords?: string[];
    page?: number;
    pageSize?: number;
  }): Promise<Envelope<Page<NewsItem>>> {
    const query: Record<string, string | number | boolean | undefined | null> = {
      market: opts?.market ?? 'CN',
      date: opts?.date,
      category: opts?.category || undefined,
      page: opts?.page ?? 1,
      page_size: opts?.pageSize ?? 50,
    };
    // keywords: list query 重复多次 &keywords=xxx
    const url = new URL(API_BASE + '/analysis/pre-open/news', window.location.origin);
    Object.entries(query).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
    });
    (opts?.keywords ?? []).forEach((k) => url.searchParams.append('keywords', k));
    return request<Page<NewsItem>>(url.toString(), { toastOnError: false });
  }

  getPreOpenFundamentals(opts?: {
    market?: string;
    date?: string;
    codes?: string;
    grade?: FundamentalGrade | '';
    page?: number;
    pageSize?: number;
  }): Promise<Envelope<Page<FundamentalProfile>>> {
    return request<Page<FundamentalProfile>>('/analysis/pre-open/fundamentals', {
      query: {
        market: opts?.market ?? 'CN',
        date: opts?.date,
        codes: opts?.codes,
        grade: opts?.grade || undefined,
        page: opts?.page ?? 1,
        page_size: opts?.pageSize ?? 50,
      },
      toastOnError: false,
    });
  }

  postPreOpenFundamentalsRun(opts: {
    codes: string;
    market?: string;
    date?: string;
  }): Promise<Envelope<{ count: number; items: FundamentalProfile[] }>> {
    return request<{ count: number; items: FundamentalProfile[] }>('/analysis/pre-open/fundamentals/run', {
      method: 'POST',
      query: {
        market: opts.market ?? 'CN',
        date: opts.date,
        codes: opts.codes,
      },
    });
  }

  getPostCloseReport(opts?: {
    market?: string;
    date?: string;
    codes?: string;
  }): Promise<Envelope<PostCloseReport>> {
    return request<PostCloseReport>('/analysis/post-close/report', {
      query: {
        market: opts?.market ?? 'CN',
        date: opts?.date,
        codes: opts?.codes,
      },
      toastOnError: false,
    });
  }

  postPostCloseRun(opts?: {
    market?: string;
    date?: string;
    force?: boolean;
  }): Promise<Envelope<PostCloseReport>> {
    return request<PostCloseReport>('/analysis/post-close/run', {
      method: 'POST',
      query: {
        market: opts?.market ?? 'CN',
        date: opts?.date,
        force: opts?.force ?? false,
      },
    });
  }

  getPostCloseLimit(opts?: {
    market?: string;
    date?: string;
    limit_type?: string;
    sector?: string;
    only_up?: 'true' | 'false' | '';
    page?: number;
    pageSize?: number;
  }): Promise<Envelope<LimitUpDownResponse>> {
    return request<LimitUpDownResponse>('/analysis/post-close/limit-up-down', {
      query: {
        market: opts?.market ?? 'CN',
        date: opts?.date,
        limit_type: opts?.limit_type,
        sector: opts?.sector,
        only_up: opts?.only_up || undefined,
        page: opts?.page ?? 1,
        page_size: opts?.pageSize ?? 50,
      },
      toastOnError: false,
    });
  }

  getPostCloseTechnical(opts?: {
    market?: string;
    codes?: string;
    page?: number;
    pageSize?: number;
  }): Promise<Envelope<Page<TechnicalAnalysis>>> {
    return request<Page<TechnicalAnalysis>>('/analysis/post-close/technical', {
      query: {
        market: opts?.market ?? 'CN',
        codes: opts?.codes,
        page: opts?.page ?? 1,
        page_size: opts?.pageSize ?? 50,
      },
      toastOnError: false,
    });
  }

  async exportPreOpenReport(
    format: 'md' | 'json',
    opts?: { market?: string; date?: string },
  ): Promise<ExportPayload> {
    const market = opts?.market ?? 'CN';
    const date = opts?.date ?? '';
    const url = `${API_BASE}/analysis/pre-open/report?export=${format}&market=${encodeURIComponent(
      market,
    )}&date=${encodeURIComponent(date)}`;
    return this._downloadAttachment(url, `pre-open-report-${market.toLowerCase()}-${date || 'today'}.${format}`);
  }

  async exportPostCloseReport(
    format: 'md' | 'json',
    opts?: { market?: string; date?: string },
  ): Promise<ExportPayload> {
    const market = opts?.market ?? 'CN';
    const date = opts?.date ?? '';
    const url = `${API_BASE}/analysis/post-close/report?export=${format}&market=${encodeURIComponent(
      market,
    )}&date=${encodeURIComponent(date)}`;
    return this._downloadAttachment(url, `post-close-report-${market.toLowerCase()}-${date || 'today'}.${format}`);
  }

  private async _downloadAttachment(url: string, fallback: string): Promise<ExportPayload> {
    let resp: Response;
    try {
      resp = await fetch(url);
    } catch {
      const msg = '导出失败：无法连接后端';
      toastError(msg);
      throw new ApiError(-1, msg);
    }
    if (!resp.ok) {
      let env: Envelope<unknown>;
      try {
        env = (await resp.json()) as Envelope<unknown>;
      } catch {
        const m = `导出失败：HTTP ${resp.status}`;
        toastError(m);
        throw new ApiError(resp.status, m);
      }
      const m = describeError(env);
      toastError(m);
      throw new ApiError(env.code, m, env.data);
    }
    const contentDisposition = resp.headers.get('Content-Disposition') || '';
    const match = contentDisposition.match(/filename\*?=(?:UTF-8''|")?([^;"]+)/i);
    const filename = match ? decodeURIComponent(match[1]) : fallback;
    const blob = await resp.blob();
    return { blob, filename };
  }
}
