/**
 * 结果对比页 store（契约 v1.3 草案 C1 + B4/P3/O3 结果拉取）。
 * - C1 任务列表：GET /backtest/jobs（limit 1..50，created_at 倒序），前端 status 过滤 + 分页。
 * - 勾选已 done 任务 → 按 action 拉各自结果（backtest→B4 / portfolio|multi→P3 / optimize→O3）
 *   归一化为 { equity, metrics, grade } 供净值叠加 + 指标对比表。
 */
import { defineStore } from 'pinia';
import { api } from '../api';
import type {
  BacktestEquityPoint,
  BacktestJobList,
  BacktestResult,
  Job,
  OptimizeResult,
  PortfolioResult,
} from '../types';
import { computeCombinedMetrics, gradeGridPoint, gradePerformance, gradePortfolio } from '../grading';
import type { GradeResult } from '../grading';
import { toastError } from '../utils/toast';

/** C1 列表状态过滤 */
export type JobStatusFilter = '' | 'done' | 'running' | 'queued' | 'failed' | 'cancelled';

/** 归一化后的可对比项（无论单标的/组合/寻优，统一为 equity + metrics + grade） */
export interface CompareItem {
  jobId: string;
  /** 展示名：action · strategy/code 摘要 */
  label: string;
  /** Job.action：backtest | portfolio | multi | optimize */
  action: string;
  /** 归一化净值曲线（optimize 无净值时为 null） */
  equity: BacktestEquityPoint[] | null;
  /** 规范化指标键（total_return/annual_return/max_drawdown/sharpe/sortino/calmar/win_rate/profit_factor/volatility/total_trades） */
  metrics: Record<string, number | null>;
  /** 评级结果（单标的/组合/寻优各自场景） */
  grade: GradeResult | null;
}

interface CompareState {
  jobs: Job[];
  total: number;
  loading: boolean;
  listError: string;
  statusFilter: JobStatusFilter;
  page: number;
  pageSize: number;
  selected: string[];
  comparing: boolean;
  compareError: string;
  items: CompareItem[];
  pollTimer: number | null;
}

/** 规范化指标键（供指标对比表固定行序） */
export const COMPARE_METRIC_KEYS: { key: string; label: string }[] = [
  { key: 'total_return', label: '总收益率' },
  { key: 'annual_return', label: '年化收益' },
  { key: 'max_drawdown', label: '最大回撤' },
  { key: 'sharpe', label: '夏普比率' },
  { key: 'sortino', label: '索提诺比率' },
  { key: 'calmar', label: '卡玛比率' },
  { key: 'win_rate', label: '胜率' },
  { key: 'profit_factor', label: '利润因子' },
  { key: 'volatility', label: '年化波动' },
  { key: 'total_trades', label: '成交笔数' },
];

export const useCompareStore = defineStore('compare', {
  state: (): CompareState => ({
    jobs: [],
    total: 0,
    loading: false,
    listError: '',
    statusFilter: '',
    page: 1,
    pageSize: 8,
    selected: [],
    comparing: false,
    compareError: '',
    items: [],
    pollTimer: null,
  }),

  getters: {
    /** 过滤 + 分页后的任务（纯前端：C1 无 offset 参数，limit 内分页） */
    filteredJobs(state): Job[] {
      const filtered =
        state.statusFilter === '' ? state.jobs : state.jobs.filter((j) => j.status === state.statusFilter);
      const start = (state.page - 1) * state.pageSize;
      return filtered.slice(start, start + state.pageSize);
    },
    filteredTotal(state): number {
      return state.statusFilter === '' ? state.jobs.length : state.jobs.filter((j) => j.status === state.statusFilter).length;
    },
    filteredTotalPages(state): number {
      return Math.max(1, Math.ceil(this.filteredTotal / state.pageSize));
    },
    canCompare(state): boolean {
      return state.selected.length >= 2;
    },
  },

  actions: {
    /** C1 拉取任务列表（limit 上限 50；分页在客户端做） */
    async loadJobs(): Promise<void> {
      if (this.loading) return;
      this.loading = true;
      this.listError = '';
      try {
        const env = await api.getBacktestJobs({ limit: 50 });
        this.jobs = env.data.items ?? [];
        this.total = env.data.count ?? this.jobs.length;
      } catch (e) {
        this.listError = e instanceof Error ? e.message : String(e);
      } finally {
        this.loading = false;
      }
    },

    setStatusFilter(filter: JobStatusFilter): void {
      this.statusFilter = filter;
      this.page = 1;
    },

    toggleSelected(jobId: string): void {
      const idx = this.selected.indexOf(jobId);
      if (idx >= 0) {
        this.selected.splice(idx, 1);
      } else {
        if (this.selected.length >= 4) {
          toastError('最多选择 4 个任务进行对比');
          return;
        }
        this.selected.push(jobId);
      }
      // 清空上一次对比结果，等待重新「对比」
      this.items = [];
      this.compareError = '';
    },

    /** 拉取所有选中任务的结果并归一化 */
    async runCompare(): Promise<void> {
      if (this.selected.length < 2) {
        this.compareError = '请至少勾选 2 个已完成的回测任务';
        return;
      }
      this.comparing = true;
      this.compareError = '';
      this.items = [];
      try {
        const items: CompareItem[] = [];
        for (const jobId of this.selected) {
          const job = this.jobs.find((j) => j.job_id === jobId);
          if (!job) continue;
          if (job.status !== 'done') {
            this.compareError = `任务 ${jobId} 尚未完成，无法对比`;
            continue;
          }
          const item = await this.fetchCompareItem(job);
          if (item) items.push(item);
        }
        this.items = items;
        if (items.length < 2) {
          this.compareError = '可对比任务不足 2 个（寻优任务若无净值曲线仅参与指标表）';
        }
      } catch (e) {
        this.compareError = e instanceof Error ? e.message : String(e);
      } finally {
        this.comparing = false;
      }
    },

    /** 按 action 拉取结果并归一化（B4/P3/O3） */
    async fetchCompareItem(job: Job): Promise<CompareItem | null> {
      const action = job.action;
      const base: Omit<CompareItem, 'metrics' | 'equity' | 'grade'> = {
        jobId: job.job_id,
        action,
        label: describeJob(job),
      };

      try {
        if (action === 'optimize') {
          const env = await api.optimizeResult(job.job_id);
          const result = env.data as OptimizeResult;
          const best = result.best;
          const metrics: Record<string, number | null> = {
            total_return: best?.total_return ?? null,
            annual_return: null,
            max_drawdown: best?.max_drawdown ?? null,
            sharpe: best?.sharpe ?? null,
            sortino: null,
            calmar: null,
            win_rate: best?.win_rate ?? null,
            profit_factor: best?.profit_factor ?? null,
            volatility: null,
            total_trades: best?.total_trades ?? null,
          };
          return {
            ...base,
            // 契约 §3.8 O3 不含净值曲线：寻优任务仅参与指标表，不参与净值叠加
            equity: null,
            metrics,
            grade: best ? gradeGridPoint(best) : null,
          };
        }

        if (action === 'portfolio' || action === 'multi') {
          const env = await api.getPortfolioResult(job.job_id);
          const result = env.data as PortfolioResult;
          const curve = result.combined_equity ?? [];
          const computed = computeCombinedMetrics(curve);
          const tp = result.total_performance ?? {};
          const metrics: Record<string, number | null> = {
            total_return: num(tp.total_return) ?? computed.total_return,
            annual_return: num(tp.annual_return) ?? computed.annual_return,
            max_drawdown: num(tp.max_drawdown) ?? computed.max_drawdown,
            sharpe: num(tp.sharpe) ?? computed.sharpe,
            sortino: num(tp.sortino) ?? computed.sortino,
            calmar: num(tp.calmar) ?? computed.calmar,
            win_rate: num(tp.win_rate),
            profit_factor: num(tp.profit_factor),
            volatility: num(tp.volatility) ?? computed.volatility,
            total_trades: num(tp.total_trades),
          };
          return {
            ...base,
            equity: curve.length ? curve : null,
            metrics,
            grade: curve.length ? gradePortfolio(curve) : null,
          };
        }

        // backtest（B4，多标的聚合 → combined）
        const env = await api.getBacktestResult(job.job_id);
        const result = env.data as BacktestResult;
        const perf = result.combined?.performance ?? {};
        const metrics: Record<string, number | null> = {
          total_return: num(perf.total_return),
          annual_return: num(perf.annual_return),
          max_drawdown: num(perf.max_drawdown),
          sharpe: num(perf.sharpe),
          sortino: num(perf.sortino),
          calmar: num(perf.calmar),
          win_rate: num(perf.win_rate),
          profit_factor: num(perf.profit_factor),
          volatility: num(perf.volatility),
          total_trades: num(perf.total_trades),
        };
        const curve = result.combined?.equity_curve ?? [];
        return {
          ...base,
          equity: curve.length ? curve : null,
          metrics,
          grade: curve.length ? gradePerformance(perf) : null,
        };
      } catch (e) {
        this.compareError = `任务 ${job.job_id} 拉取失败：${e instanceof Error ? e.message : String(e)}`;
        return null;
      }
    },

    resetCompare(): void {
      this.selected = [];
      this.items = [];
      this.compareError = '';
    },
  },
});

/** Job → 展示名（action · 摘要） */
function describeJob(job: Job): string {
  const summary = (job.result_summary ?? {}) as Record<string, unknown>;
  const strategy = typeof summary.strategy === 'string' ? summary.strategy : '';
  const code = typeof summary.code === 'string' ? summary.code : '';
  const codes = Array.isArray(summary.codes) ? (summary.codes as string[]).join(',') : '';
  const brief = code || codes || strategy || job.job_id.slice(0, 8);
  return `${job.action} · ${brief}`;
}

/** 兼容后端 null/undefined 的数字取值 */
function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}
