/**
 * 因子分析 store（契约 §5.2）：因子库列表、因子报告、计算 Job、合成模型。
 */
import { defineStore } from 'pinia';
import { api } from '../api';
import { ApiError } from '../api/types';
import type {
  ComputeRequest,
  CombineRequest,
  FactorInfo,
  FactorModel,
  FactorReport,
  Job,
} from '../types';

interface FactorState {
  factors: FactorInfo[];
  factorsTotal: number;
  factorsLoading: boolean;
  factorsError: string;
  selectedName: string | null;
  report: FactorReport | null;
  reportJob: Job | null;
  reportLoading: boolean;
  reportError: string;
  /** 报告 404「未计算」标志：引导用户先运行 compute（问题 4） */
  reportNeedsCompute: boolean;
  reportPolling: boolean;
  reportTimer: number | null;
  models: FactorModel[];
  modelsLoading: boolean;
  computeJob: Job | null;
  computePolling: boolean;
  computeTimer: number | null;
  combineLoading: boolean;
  combineJob: Job | null;
  combineTimer: number | null;
  combineResult: FactorModel | null;
}

export const useFactorStore = defineStore('factor', {
  state: (): FactorState => ({
    factors: [],
    factorsTotal: 0,
    factorsLoading: false,
    factorsError: '',
    selectedName: null,
    report: null,
    reportJob: null,
    reportLoading: false,
    reportError: '',
    reportNeedsCompute: false,
    reportPolling: false,
    reportTimer: null,
    models: [],
    modelsLoading: false,
    computeJob: null,
    computePolling: false,
    computeTimer: null,
    combineLoading: false,
    combineJob: null,
    combineTimer: null,
    combineResult: null,
  }),

  getters: {
    selectedFactor(state): FactorInfo | null {
      return state.factors.find((f) => f.name === state.selectedName) ?? null;
    },
    computedFactors(state): FactorInfo[] {
      return state.factors.filter((f) => f.status === 'computed');
    },
  },

  actions: {
    async loadFactors(): Promise<void> {
      this.factorsLoading = true;
      this.factorsError = '';
      try {
        const env = await api.getFactors('CN', 1, 500);
        this.factors = env.data.items;
        this.factorsTotal = env.data.total;
        if (!this.selectedName && this.factors.length > 0) {
          this.selectedName = this.factors[0].name;
        }
      } catch (e) {
        this.factorsError = e instanceof Error ? e.message : String(e);
      } finally {
        this.factorsLoading = false;
      }
    },

    async selectFactor(name: string): Promise<void> {
      if (this.selectedName === name) return;
      this.selectedName = name;
      await this.loadReport(name);
    },

    async loadReport(name?: string): Promise<void> {
      const target = name ?? this.selectedName;
      if (!target) return;
      this.stopReportPolling();
      this.reportLoading = true;
      this.reportJob = null;
      this.reportError = '';
      this.reportNeedsCompute = false;
      this.report = null;
      try {
        // F4 已改为后台异步 Job（子进程隔离），立即拿到 job_id 后轮询结果。
        const env = await api.postFactorReport({ name: target, market: 'CN' });
        this.reportJob = env.data;
        this.startReportPolling();
      } catch (e) {
        this.reportError = e instanceof Error ? e.message : String(e);
        // 404 = 因子不存在
        this.reportNeedsCompute = e instanceof ApiError && e.code === 404;
        this.reportLoading = false;
        this.report = null;
      }
    },

    startReportPolling(): void {
      if (this.reportPolling) return;
      if (!this.reportJob) {
        this.stopReportPolling();
        return;
      }
      this.reportPolling = true;
      const tick = async (): Promise<void> => {
        if (!this.reportJob) {
          this.stopReportPolling();
          return;
        }
        try {
          const env = await api.getFactorJob(this.reportJob.job_id);
          this.reportJob = env.data;
          const status = env.data.status;
          if (status !== 'queued' && status !== 'running') {
            this.stopReportPolling();
            this.reportLoading = false;
            if (status === 'done' && env.data.result_summary) {
              this.report = env.data.result_summary as unknown as FactorReport;
            } else if (status === 'failed') {
              const msg = env.data.error?.message ?? '报告生成失败';
              this.reportError = msg;
              // 无已计算数据 → 引导先 compute（问题 4），不裸报错
              this.reportNeedsCompute = /无已计算数据|请先 compute|尚未计算/i.test(msg);
              this.report = null;
            } else {
              this.reportError = status === 'cancelled' ? '报告任务已取消' : '报告生成失败';
              this.report = null;
            }
          }
        } catch {
          this.stopReportPolling();
          this.reportLoading = false;
        }
      };
      void tick();
      this.reportTimer = window.setInterval(() => {
        void tick();
      }, 1200);
    },

    stopReportPolling(): void {
      this.reportPolling = false;
      if (this.reportTimer !== null) {
        window.clearInterval(this.reportTimer);
        this.reportTimer = null;
      }
    },

    async compute(req: ComputeRequest): Promise<void> {
      const env = await api.postFactorCompute(req);
      this.computeJob = env.data;
      this.startComputePolling();
    },

    startComputePolling(): void {
      if (this.computePolling) return;
      this.computePolling = true;
      const tick = async (): Promise<void> => {
        if (!this.computeJob) {
          this.stopComputePolling();
          return;
        }
        try {
          const env = await api.getFactorJob(this.computeJob.job_id);
          this.computeJob = env.data;
          if (env.data.status !== 'queued' && env.data.status !== 'running') {
            this.stopComputePolling();
            void this.loadFactors();
            if (this.selectedName) void this.loadReport(this.selectedName);
          }
        } catch {
          this.stopComputePolling();
        }
      };
      void tick();
      this.computeTimer = window.setInterval(() => {
        void tick();
      }, 1500);
    },

    stopComputePolling(): void {
      if (this.computeTimer !== null) {
        window.clearInterval(this.computeTimer);
        this.computeTimer = null;
      }
      this.computePolling = false;
    },

    async combine(req: CombineRequest): Promise<void> {
      this.combineLoading = true;
      this.combineResult = null;
      try {
        // F5 已改为后台异步 Job（子进程隔离），立即拿到 job_id 后轮询结果。
        const env = await api.postFactorCombine(req);
        this.combineJob = env.data;
        this.startCombinePolling(req.save_model);
      } finally {
        this.combineLoading = false;
      }
    },

    startCombinePolling(saveModel: boolean): void {
      if (!this.combineJob) {
        this.stopCombinePolling();
        return;
      }
      const tick = async (): Promise<void> => {
        if (!this.combineJob) {
          this.stopCombinePolling();
          return;
        }
        try {
          const env = await api.getFactorJob(this.combineJob.job_id);
          this.combineJob = env.data;
          const status = env.data.status;
          if (status !== 'queued' && status !== 'running') {
            this.stopCombinePolling();
            const summary = env.data.result_summary as FactorModel | null;
            if (status === 'done' && summary) {
              this.combineResult = summary;
              if (saveModel) void this.loadModels();
            }
          }
        } catch {
          this.stopCombinePolling();
        }
      };
      void tick();
      this.combineTimer = window.setInterval(() => {
        void tick();
      }, 1500);
    },

    stopCombinePolling(): void {
      if (this.combineTimer !== null) {
        window.clearInterval(this.combineTimer);
        this.combineTimer = null;
      }
    },

    async loadModels(): Promise<void> {
      this.modelsLoading = true;
      try {
        const env = await api.getFactorModels('CN', 1, 100);
        this.models = env.data.items;
      } catch {
        // 模型列表加载失败静默（页面呈现空态）
      } finally {
        this.modelsLoading = false;
      }
    },
  },
});
