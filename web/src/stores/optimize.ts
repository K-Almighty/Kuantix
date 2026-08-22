/**
 * 参数寻优页 store（契约 v1.3 草案 O1–O3）：策略列表 / 提交寻优 Job / 轮询 / 完整结果。
 * Job 模式与 backtest 一致：提交 → 轮询（1s）→ done 后拉 O3 结果。
 */
import { defineStore } from 'pinia';
import { api } from '../api';
import type {
  BacktestStrategySchema,
  OptimizeResult,
  OptimizeRunRequest,
  OptimizeAllResult,
  OptimizeAllRunRequest,
} from '../types';
import type { Job } from '../types/data';
import { toastError } from '../utils/toast';

interface OptimizeState {
  schemas: BacktestStrategySchema[];
  schemasError: string;
  running: boolean;
  job: Job | null;
  result: OptimizeResult | null;
  resultError: string;
  /** 最近一次提交的请求快照（供「查看回测」/跳转联动） */
  lastReq: OptimizeRunRequest | null;
  pollTimer: number | null;
  /** 一键寻优所有策略（O4/O5） */
  allRunning: boolean;
  allJob: Job | null;
  allResult: OptimizeAllResult | null;
  allError: string;
  allPollTimer: number | null;
}

export const useOptimizeStore = defineStore('optimize', {
  state: (): OptimizeState => ({
    schemas: [],
    schemasError: '',
    running: false,
    job: null,
    result: null,
    resultError: '',
    lastReq: null,
    pollTimer: null,
    allRunning: false,
    allJob: null,
    allResult: null,
    allError: '',
    allPollTimer: null,
  }),

  actions: {
    /** B1 策略注册表（寻优参数 schema + preset_grid 预设网格） */
    async loadStrategies(): Promise<void> {
      try {
        const env = await api.getBacktestStrategies();
        this.schemas = env.data.items;
        this.schemasError = '';
      } catch (e) {
        this.schemasError = e instanceof Error ? e.message : String(e);
      }
    },

    /** O1 提交寻优 → Job → 轮询 */
    async run(req: OptimizeRunRequest): Promise<void> {
      this.stopPolling();
      this.running = true;
      this.result = null;
      this.resultError = '';
      this.lastReq = { ...req };
      try {
        const env = await api.optimizeRun(req);
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

    /** O2 进度轮询（静默失败，由页面呈现 job 状态） */
    async poll(): Promise<void> {
      if (!this.job) return;
      try {
        const env = await api.optimizeJob(this.job.job_id);
        this.job = env.data;
        if (env.data.status === 'done') {
          this.stopPolling();
          this.running = false;
          await this.loadResult(env.data.job_id);
        } else if (env.data.status === 'failed' || env.data.status === 'cancelled') {
          this.stopPolling();
          this.running = false;
          this.resultError = env.data.error?.message ?? `寻优任务${env.data.status}（无错误详情）`;
        }
      } catch (e) {
        this.resultError = e instanceof Error ? e.message : String(e);
      }
    },

    /** O3 拉取完整结果 */
    async loadResult(jobId: string): Promise<void> {
      try {
        const env = await api.optimizeResult(jobId);
        this.result = env.data;
        this.resultError = '';
      } catch (e) {
        this.resultError = e instanceof Error ? e.message : String(e);
      }
    },

    /** O6 删除单个策略寻优（job + 结果），成功后清理内存状态 */
    async remove(jobId: string): Promise<void> {
      await api.deleteOptimizeJob(jobId);
      // 仅当删除的是当前展示的 session 时清理内存
      if (this.job && this.job.job_id === jobId) {
        this.stopPolling();
        this.running = false;
        this.job = null;
        this.result = null;
        this.resultError = '';
      }
      if (this.allJob && this.allJob.job_id === jobId) {
        this.stopAllPolling();
        this.allRunning = false;
        this.allJob = null;
        this.allResult = null;
        this.allError = '';
      }
    },

    /* -------- 一键寻优所有策略（O4/O5） -------- */

    /** O4 提交一键寻优所有策略 → Job → 轮询 */
    async runAll(req: OptimizeAllRunRequest): Promise<void> {
      this.stopAllPolling();
      this.allRunning = true;
      this.allResult = null;
      this.allError = '';
      try {
        const env = await api.optimizeAllRun(req);
        this.allJob = env.data;
        this.startAllPolling();
      } catch (e) {
        this.allRunning = false;
        toastError(e instanceof Error ? e.message : String(e));
        throw e;
      }
    },

    startAllPolling(): void {
      if (this.allPollTimer !== null) return;
      this.allPollTimer = window.setInterval(() => {
        void this.pollAll();
      }, 1200);
    },

    stopAllPolling(): void {
      if (this.allPollTimer !== null) {
        window.clearInterval(this.allPollTimer);
        this.allPollTimer = null;
      }
    },

    async pollAll(): Promise<void> {
      if (!this.allJob) return;
      try {
        const env = await api.optimizeJob(this.allJob.job_id);
        this.allJob = env.data;
        if (env.data.status === 'done') {
          this.stopAllPolling();
          this.allRunning = false;
          await this.loadAllResult(env.data.job_id);
        } else if (env.data.status === 'failed' || env.data.status === 'cancelled') {
          this.stopAllPolling();
          this.allRunning = false;
          this.allError = env.data.error?.message ?? `一键寻优任务${env.data.status}（无错误详情）`;
        }
      } catch (e) {
        this.allError = e instanceof Error ? e.message : String(e);
      }
    },

    /** O5 拉取一键寻优所有策略结果 */
    async loadAllResult(jobId: string): Promise<void> {
      try {
        const env = await api.optimizeAllResult(jobId);
        this.allResult = env.data;
        this.allError = '';
      } catch (e) {
        this.allError = e instanceof Error ? e.message : String(e);
      }
    },

    reset(): void {
      this.stopPolling();
      this.stopAllPolling();
      this.running = false;
      this.job = null;
      this.result = null;
      this.resultError = '';
      this.allRunning = false;
      this.allJob = null;
      this.allResult = null;
      this.allError = '';
    },
  },
});
