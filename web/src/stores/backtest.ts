/**
 * 回测页 store（契约 §3.6 B1–B4）：策略列表 / 运行 Job / 轮询 / 完整结果。
 * 与 factor/screen 的 Job 模式一致：提交 → 轮询（1s）→ done 后拉结果。
 */
import { defineStore } from 'pinia';
import { api } from '../api';
import type {
  BacktestJobSummary,
  BacktestResult,
  BacktestRunRequest,
  BacktestStrategySchema,
} from '../types';
import type { Job } from '../types/data';
import { toastError } from '../utils/toast';

/** 从参数寻优页「查看」跳转带来的回测预填上下文 */
export interface BacktestPreset {
  strategy: string;
  params: Record<string, number | string>;
  codes: string[];
  startDate: string;
  endDate: string;
  /** 预填后是否自动触发一次回测（用于「查看交易明细」直出） */
  autoRun?: boolean;
}

interface BacktestState {
  strategies: BacktestStrategySchema[];
  strategiesError: string;
  running: boolean;
  job: Job | null;
  result: BacktestResult | null;
  resultError: string;
  pollTimer: number | null;
  /** 参数寻优页「查看」跳转时设置的回测预填上下文（跨路由保留） */
  preset: BacktestPreset | null;
}

export const useBacktestStore = defineStore('backtest', {
  state: (): BacktestState => ({
    strategies: [],
    strategiesError: '',
    running: false,
    job: null,
    result: null,
    resultError: '',
    pollTimer: null,
    preset: null,
  }),

  getters: {
    jobSummary(state): BacktestJobSummary | null {
      if (!state.job || state.job.status !== 'done' || !state.job.result_summary) return null;
      return state.job.result_summary as unknown as BacktestJobSummary;
    },
  },

  actions: {
    /** 设置「查看」预填上下文（由参数寻优页调用） */
    setPreset(preset: BacktestPreset): void {
      this.preset = preset;
    },
    /** 读取并清空预填上下文（由 Backtest 页 onMounted 消费） */
    consumePreset(): BacktestPreset | null {
      const p = this.preset;
      this.preset = null;
      return p;
    },
    /** 清空已展示的回测结果（避免跳转后展示上一次旧策略的交易明细） */
    clearResult(): void {
      this.stopPolling();
      this.running = false;
      this.result = null;
      this.resultError = '';
      this.job = null;
    },

    async loadStrategies(): Promise<void> {
      try {
        const env = await api.getBacktestStrategies();
        this.strategies = env.data.items;
        this.strategiesError = '';
      } catch (e) {
        this.strategiesError = e instanceof Error ? e.message : String(e);
      }
    },

    async run(req: BacktestRunRequest): Promise<void> {
      this.stopPolling();
      this.running = true;
      this.result = null;
      this.resultError = '';
      try {
        const env = await api.postBacktestRun(req);
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

    async poll(): Promise<void> {
      if (!this.job) return;
      try {
        const env = await api.getBacktestJob(this.job.job_id);
        this.job = env.data;
        if (env.data.status === 'done') {
          this.stopPolling();
          this.running = false;
          await this.loadResult(env.data.job_id);
        } else if (env.data.status === 'failed' || env.data.status === 'cancelled') {
          this.stopPolling();
          this.running = false;
          this.resultError =
            env.data.error?.message ?? `回测任务${env.data.status}（无错误详情）`;
        }
      } catch (e) {
        // 轮询失败静默（由页面呈现 job 状态），不弹 toast
        this.resultError = e instanceof Error ? e.message : String(e);
      }
    },

    async loadResult(jobId: string): Promise<void> {
      try {
        const env = await api.getBacktestResult(jobId);
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
