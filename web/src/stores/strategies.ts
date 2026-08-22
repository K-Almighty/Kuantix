/**
 * 策略库页 store（契约 v1.3 草案 S1–S5）：列表（分页+kind 过滤）/ 新建 / 详情 / 删除 / 多策略组合回测。
 * 多策略组合回测 Job 轮询复用 P2/P3（job_id 全局唯一、结果同落 BacktestResultStore，结构同 PortfolioResult）。
 */
import { defineStore } from 'pinia';
import { api } from '../api';
import type {
  BacktestStrategySchema,
  MultiStrategyRunRequest,
  PortfolioResult,
  SavedStrategy,
  SavedStrategyCreate,
  StrategyKind,
} from '../types';
import type { Job } from '../types/data';
import { toastError } from '../utils/toast';

interface StrategiesState {
  items: SavedStrategy[];
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  loading: boolean;
  listError: string;
  creating: boolean;
  deletingId: string;
  detail: SavedStrategy | null;
  detailError: string;
  detailLoading: boolean;
  /* B1 策略注册表（新建表单策略下拉/参数预设） */
  schemas: BacktestStrategySchema[];
  schemasError: string;
  /* 多策略组合回测（S5） */
  running: boolean;
  job: Job | null;
  result: PortfolioResult | null;
  resultError: string;
  pollTimer: number | null;
}

export const useStrategiesStore = defineStore('strategies', {
  state: (): StrategiesState => ({
    items: [],
    page: 1,
    pageSize: 20,
    total: 0,
    totalPages: 1,
    loading: false,
    listError: '',
    creating: false,
    deletingId: '',
    detail: null,
    detailError: '',
    detailLoading: false,
    schemas: [],
    schemasError: '',
    running: false,
    job: null,
    result: null,
    resultError: '',
    pollTimer: null,
  }),

  actions: {
    /** B1 策略注册表（供新建表单选择策略 + 参数预设） */
    async loadSchemas(): Promise<void> {
      try {
        const env = await api.getBacktestStrategies();
        this.schemas = env.data.items;
        this.schemasError = '';
      } catch (e) {
        this.schemasError = e instanceof Error ? e.message : String(e);
      }
    },

    /** S1 列表（分页，kind 可空=全部） */
    async loadList(kind: StrategyKind | '' = '', page = 1, pageSize = 20): Promise<void> {
      if (this.loading) return;
      this.loading = true;
      this.listError = '';
      try {
        const env = await api.getStrategies({ kind, page, pageSize });
        this.items = env.data.items;
        this.page = env.data.page;
        this.pageSize = env.data.page_size;
        this.total = env.data.total;
        this.totalPages = env.data.total_pages;
      } catch (e) {
        this.listError = e instanceof Error ? e.message : String(e);
      } finally {
        this.loading = false;
      }
    },

    /** S2 新建 */
    async create(req: SavedStrategyCreate): Promise<SavedStrategy> {
      this.creating = true;
      try {
        const env = await api.createStrategy(req);
        return env.data;
      } catch (e) {
        toastError(e instanceof Error ? e.message : String(e));
        throw e;
      } finally {
        this.creating = false;
      }
    },

    /** S3 详情 */
    async loadDetail(strategyId: string): Promise<SavedStrategy | null> {
      this.detailLoading = true;
      this.detailError = '';
      try {
        const env = await api.getStrategy(strategyId);
        this.detail = env.data;
        return env.data;
      } catch (e) {
        this.detailError = e instanceof Error ? e.message : String(e);
        return null;
      } finally {
        this.detailLoading = false;
      }
    },

    /** S4 删除（fail-loud：不存在→404，不静默成功） */
    async remove(strategyId: string): Promise<void> {
      this.deletingId = strategyId;
      try {
        await api.deleteStrategy(strategyId);
        // 从本地列表移除，避免整页刷新
        this.items = this.items.filter((s) => s.id !== strategyId);
        this.total = Math.max(0, this.total - 1);
      } catch (e) {
        toastError(e instanceof Error ? e.message : String(e));
        throw e;
      } finally {
        this.deletingId = '';
      }
    },

    /** S5 提交多策略组合回测 → Job → 轮询 */
    async runMulti(req: MultiStrategyRunRequest): Promise<void> {
      this.stopPolling();
      this.running = true;
      this.result = null;
      this.resultError = '';
      try {
        const env = await api.strategiesRunMulti(req);
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

    /** 进度轮询（复用 P2 /portfolio/jobs/{id}，静默失败） */
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
          this.resultError = env.data.error?.message ?? `多策略组合回测任务${env.data.status}（无错误详情）`;
        }
      } catch (e) {
        this.resultError = e instanceof Error ? e.message : String(e);
      }
    },

    /** 结果拉取（复用 P3 /portfolio/results/{id}，结构同 PortfolioResult，key="{label}@{symbol}"） */
    async loadResult(jobId: string): Promise<void> {
      try {
        const env = await api.getPortfolioResult(jobId);
        this.result = env.data;
        this.resultError = '';
      } catch (e) {
        this.resultError = e instanceof Error ? e.message : String(e);
      }
    },

    resetRun(): void {
      this.stopPolling();
      this.running = false;
      this.job = null;
      this.result = null;
      this.resultError = '';
    },

    clearDetail(): void {
      this.detail = null;
      this.detailError = '';
    },
  },
});
