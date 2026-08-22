/**
 * 组合回测页 store（契约 v1.3 草案 P1–P3）：策略列表 / 组合回测 Job / 轮询 / 完整结果。
 * Job 模式与 backtest/factor/screen 一致：提交 → 轮询（1s）→ done 后拉结果。
 * 组合回测 = 1 策略 × N 标的，资金分仓（total_cash/N），结果结构 PortfolioResult。
 */
import { defineStore } from 'pinia';
import { api } from '../api';
import type {
  BacktestStrategySchema,
  PortfolioResult,
  PortfolioRunRequest,
} from '../types';
import type { Job } from '../types/data';
import { toastError } from '../utils/toast';

interface PortfolioState {
  strategies: BacktestStrategySchema[];
  strategiesError: string;
  strategiesLoading: boolean;
  running: boolean;
  job: Job | null;
  result: PortfolioResult | null;
  resultError: string;
  pollTimer: number | null;
}

export const usePortfolioStore = defineStore('portfolio', {
  state: (): PortfolioState => ({
    strategies: [],
    strategiesError: '',
    strategiesLoading: false,
    running: false,
    job: null,
    result: null,
    resultError: '',
    pollTimer: null,
  }),

  actions: {
    /** B1 策略注册表（组合=1 策略 × N 标的，策略下拉复用） */
    async loadStrategies(): Promise<void> {
      if (this.strategiesLoading) return;
      this.strategiesLoading = true;
      try {
        const env = await api.getBacktestStrategies();
        this.strategies = env.data.items;
        this.strategiesError = '';
      } catch (e) {
        this.strategiesError = e instanceof Error ? e.message : String(e);
      } finally {
        this.strategiesLoading = false;
      }
    },

    /** P1 提交组合回测 → Job → 开始轮询 */
    async run(req: PortfolioRunRequest): Promise<void> {
      this.stopPolling();
      this.running = true;
      this.result = null;
      this.resultError = '';
      try {
        const env = await api.portfolioRun(req);
        this.job = env.data;
        this.startPolling();
      } catch (e) {
        this.running = false;
        toastError(e instanceof Error ? e.message : String(e));
        throw e;
      }
    },

    startPolling(): void {
      if (this.pollTimer !== null) return;
      this.pollTimer = window.setInterval(() => {
        void this.poll();
      }, 1000);
    },

    stopPolling(): void {
      if (this.pollTimer !== null) {
        window.clearInterval(this.pollTimer);
        this.pollTimer = null;
      }
    },

    /** P2 进度轮询（静默失败，由页面呈现 job/错误态） */
    async poll(): Promise<void> {
      if (!this.job) return;
      try {
        const env = await api.getPortfolioJob(this.job.job_id);
        this.job = env.data;
        if (env.data.status === 'done') {
          this.stopPolling();
          this.running = false;
          await this.loadResult(env.data.job_id);
        } else if (env.data.status === 'failed' || env.data.status === 'cancelled') {
          this.stopPolling();
          this.running = false;
          this.resultError = env.data.error?.message ?? `组合回测任务${env.data.status}（无错误详情）`;
        }
      } catch (e) {
        this.resultError = e instanceof Error ? e.message : String(e);
      }
    },

    /** P3 拉取完整 PortfolioResult */
    async loadResult(jobId: string): Promise<void> {
      try {
        const env = await api.getPortfolioResult(jobId);
        this.result = env.data;
        this.resultError = '';
      } catch (e) {
        this.resultError = e instanceof Error ? e.message : String(e);
      }
    },

    reset(): void {
      this.stopPolling();
      this.running = false;
      this.job = null;
      this.result = null;
      this.resultError = '';
    },
  },
});
