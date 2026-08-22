/**
 * 选股结果 store（契约 §5.4）：条件插件、模型、批次、结果分页/排序、运行 Job、导出。
 */
import { defineStore } from 'pinia';
import { api } from '../api';
import type { ExportPayload } from '../api/types';
import type {
  FilterInfo,
  FactorInfo,
  FactorModel,
  Page,
  ScreenBatch,
  ScreenResultView,
  ScreenRunRequest,
  ScreenFactorRunRequest,
  Job,
} from '../types';

interface ScreenState {
  mode: 'model' | 'factor';
  filters: FilterInfo[];
  filtersLoading: boolean;
  filtersError: string;
  models: FactorModel[];
  modelsLoading: boolean;
  modelsError: string;
  factors: FactorInfo[];
  factorsLoading: boolean;
  factorsError: string;
  factorResults: Page<ScreenResultView> | null;
  factorResultsLoading: boolean;
  factorResultsError: string;
  batches: Page<ScreenBatch> | null;
  batchesLoading: boolean;
  selectedBatchId: string | null;
  results: Page<ScreenResultView> | null;
  resultsLoading: boolean;
  resultsError: string;
  sortBy: string;
  order: 'asc' | 'desc';
  runJob: Job | null;
  runPolling: boolean;
  runTimer: number | null;
}

export const useScreenStore = defineStore('screen', {
  state: (): ScreenState => ({
    mode: 'model',
    filters: [],
    filtersLoading: false,
    filtersError: '',
    models: [],
    modelsLoading: false,
    modelsError: '',
    factors: [],
    factorsLoading: false,
    factorsError: '',
    factorResults: null,
    factorResultsLoading: false,
    factorResultsError: '',
    batches: null,
    batchesLoading: false,
    selectedBatchId: null,
    results: null,
    resultsLoading: false,
    resultsError: '',
    sortBy: 'score',
    order: 'desc',
    runJob: null,
    runPolling: false,
    runTimer: null,
  }),

  actions: {
    async init(): Promise<void> {
      await Promise.all([
        this.loadFilters(),
        this.loadModels(),
        this.loadFactors(),
        this.loadBatches(),
      ]);
    },

    async loadFactors(): Promise<void> {
      this.factorsLoading = true;
      this.factorsError = '';
      try {
        const env = await api.getFactors('CN', 1, 500);
        this.factors = env.data.items;
      } catch (e) {
        this.factorsError = e instanceof Error ? e.message : String(e);
      } finally {
        this.factorsLoading = false;
      }
    },

    async loadFilters(): Promise<void> {
      this.filtersLoading = true;
      this.filtersError = '';
      try {
        const env = await api.getScreenFilters('CN');
        this.filters = env.data.items;
      } catch (e) {
        this.filtersError = e instanceof Error ? e.message : String(e);
      } finally {
        this.filtersLoading = false;
      }
    },

    async loadModels(): Promise<void> {
      this.modelsLoading = true;
      this.modelsError = '';
      try {
        const env = await api.getFactorModels('CN', 1, 100);
        this.models = env.data.items;
      } catch (e) {
        this.modelsError = e instanceof Error ? e.message : String(e);
      } finally {
        this.modelsLoading = false;
      }
    },

    async loadBatches(): Promise<void> {
      this.batchesLoading = true;
      try {
        const env = await api.getScreenBatches('CN', 1, 20);
        this.batches = env.data;
        if (!this.selectedBatchId && this.batches.items.length > 0) {
          this.selectedBatchId = this.batches.items[0].batch_id;
          void this.loadResults(this.batches.items[0].batch_id);
        }
      } catch {
        // 静默：页面呈现错误/空态
      } finally {
        this.batchesLoading = false;
      }
    },

    async run(req: ScreenRunRequest): Promise<void> {
      const env = await api.postScreenRun(req);
      this.runJob = env.data;
      this.startRunPolling();
    },

    async runFactor(req: ScreenFactorRunRequest): Promise<void> {
      this.factorResultsLoading = true;
      this.factorResultsError = '';
      try {
        const env = await api.screenFactorRun(req);
        this.factorResults = env.data;
      } catch (e) {
        this.factorResultsError = e instanceof Error ? e.message : String(e);
        this.factorResults = null;
      } finally {
        this.factorResultsLoading = false;
      }
    },

    startRunPolling(): void {
      if (this.runPolling) return;
      this.runPolling = true;
      const tick = async (): Promise<void> => {
        if (!this.runJob) {
          this.stopRunPolling();
          return;
        }
        try {
          const env = await api.getScreenJob(this.runJob.job_id);
          this.runJob = env.data;
          if (env.data.status !== 'queued' && env.data.status !== 'running') {
            this.stopRunPolling();
            const summary = env.data.result_summary;
            if (env.data.status === 'done' && summary && typeof summary === 'object' && 'batch_id' in summary) {
              this.selectedBatchId = String(summary.batch_id);
              await this.loadResults(this.selectedBatchId);
              await this.loadBatches();
            }
          }
        } catch {
          this.stopRunPolling();
        }
      };
      void tick();
      this.runTimer = window.setInterval(() => {
        void tick();
      }, 1500);
    },

    stopRunPolling(): void {
      if (this.runTimer !== null) {
        window.clearInterval(this.runTimer);
        this.runTimer = null;
      }
      this.runPolling = false;
    },

    async loadResults(batchId?: string, opts: { page?: number; pageSize?: number } = {}): Promise<void> {
      const target = batchId ?? this.selectedBatchId;
      if (!target) return;
      this.resultsLoading = true;
      this.resultsError = '';
      try {
        const env = await api.getScreenResults(target, {
          page: opts.page ?? this.results?.page ?? 1,
          pageSize: opts.pageSize ?? this.results?.page_size ?? 50,
          sortBy: this.sortBy,
          order: this.order,
        });
        this.results = env.data;
      } catch (e) {
        this.resultsError = e instanceof Error ? e.message : String(e);
        this.results = null;
      } finally {
        this.resultsLoading = false;
      }
    },

    async setSort(sortBy: string, order: 'asc' | 'desc'): Promise<void> {
      if (this.sortBy === sortBy) {
        this.order = this.order === 'asc' ? 'desc' : 'asc';
      } else {
        this.sortBy = sortBy;
        this.order = order;
      }
      await this.loadResults();
    },

    async goPage(page: number): Promise<void> {
      await this.loadResults(undefined, { page });
    },

    exportResults(batchId: string, format: 'json' | 'csv'): Promise<ExportPayload> {
      return api.exportScreenResults(batchId, format, 'CN');
    },
  },
});
