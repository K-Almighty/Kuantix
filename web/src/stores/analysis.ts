/**
 * 盘前分析 / 盘后复盘 store。
 * 两个视图各自有独立 state：报告（getReport） + 列表分页（news/fundamentals/limit/tech）。
 * 不做自动轮询：报告数据是每天定时生成（盘前 08:30、盘后 15:20），视图进入时拉一次 + 手动重算按钮。
 */
import { defineStore } from 'pinia';
import { api } from '../api';
import type {
  NewsItem,
  FundamentalProfile,
  TechnicalAnalysis,
  PreOpenReport,
  PostCloseReport,
  LimitUpDownResponse,
  NewsCategory,
  FundamentalGrade,
  Page,
} from '../types';
import { triggerBlobDownload } from '../utils/download';
import { toastSuccess, toastError } from '../utils/toast';

/* -------- 盘前 -------- */

interface PreOpenOnlyState {
  preReport: PreOpenReport | null;
  preReportLoading: boolean;
  preReportError: string;
  news: Page<NewsItem> | null;
  newsLoading: boolean;
  newsPage: number;
  newsPageSize: number;
  newsCategory: NewsCategory | '';
  newsKeywords: string; // 逗号分隔，视图输入 → split
  fundamentals: Page<FundamentalProfile> | null;
  fundamentalsLoading: boolean;
  fundamentalsPage: number;
  fundamentalsPageSize: number;
  fundamentalsGrade: FundamentalGrade | '';
  fundamentalsCodes: string; // 逗号分隔（过滤用）
}

/* -------- 盘后 -------- */

interface PostCloseOnlyState {
  postReport: PostCloseReport | null;
  postReportLoading: boolean;
  postReportError: string;
  limit: LimitUpDownResponse | null;
  limitLoading: boolean;
  limitPage: number;
  limitPageSize: number;
  limitType: string;
  limitSector: string;
  limitSide: 'all' | 'up' | 'down';
  technical: Page<TechnicalAnalysis> | null;
  technicalLoading: boolean;
  technicalPage: number;
  technicalPageSize: number;
  technicalCodes: string;
}

interface CommonState {
  date: string;
  market: string;
}

const DEFAULT_PAGE_SIZE = 50;

export const useAnalysisStore = defineStore('analysis', {
  state: (): CommonState & PreOpenOnlyState & PostCloseOnlyState => ({
    date: '',
    market: 'CN',
    // pre
    preReport: null,
    preReportLoading: false,
    preReportError: '',
    news: null,
    newsLoading: false,
    newsPage: 1,
    newsPageSize: DEFAULT_PAGE_SIZE,
    newsCategory: '',
    newsKeywords: '',
    fundamentals: null,
    fundamentalsLoading: false,
    fundamentalsPage: 1,
    fundamentalsPageSize: DEFAULT_PAGE_SIZE,
    fundamentalsGrade: '',
    fundamentalsCodes: '',
    // post
    postReport: null,
    postReportLoading: false,
    postReportError: '',
    limit: null,
    limitLoading: false,
    limitPage: 1,
    limitPageSize: DEFAULT_PAGE_SIZE,
    limitType: '',
    limitSector: '',
    limitSide: 'all',
    technical: null,
    technicalLoading: false,
    technicalPage: 1,
    technicalPageSize: DEFAULT_PAGE_SIZE,
    technicalCodes: '',
  }),

  actions: {
    /* ============================================================
     * 盘前
     * ============================================================ */
    async loadPreOpenReport(force = false) {
      this.preReportLoading = true;
      this.preReportError = '';
      try {
        if (force) {
          const env = await api.postPreOpenRun({ market: this.market, date: this.date || undefined });
          this.preReport = env.data;
        } else {
          const env = await api.getPreOpenReport({ market: this.market, date: this.date || undefined });
          this.preReport = env.data;
        }
      } catch (e) {
        this.preReport = null;
        this.preReportError = (e as Error).message || '获取盘前报告失败';
      } finally {
        this.preReportLoading = false;
      }
    },

    async loadNews(page?: number, pageSize?: number) {
      if (page !== undefined) this.newsPage = page;
      if (pageSize !== undefined) this.newsPageSize = pageSize;
      this.newsLoading = true;
      try {
        const keywords = this.newsKeywords
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean);
        const env = await api.getPreOpenNews({
          market: this.market,
          date: this.date || undefined,
          category: this.newsCategory || undefined,
          keywords,
          page: this.newsPage,
          pageSize: this.newsPageSize,
        });
        this.news = env.data;
      } catch (e) {
        toastError((e as Error).message || '加载消息面失败');
      } finally {
        this.newsLoading = false;
      }
    },

    async loadFundamentals(page?: number, pageSize?: number) {
      if (page !== undefined) this.fundamentalsPage = page;
      if (pageSize !== undefined) this.fundamentalsPageSize = pageSize;
      this.fundamentalsLoading = true;
      try {
        const env = await api.getPreOpenFundamentals({
          market: this.market,
          date: this.date || undefined,
          codes: this.fundamentalsCodes || undefined,
          grade: this.fundamentalsGrade || undefined,
          page: this.fundamentalsPage,
          pageSize: this.fundamentalsPageSize,
        });
        this.fundamentals = env.data;
      } catch (e) {
        toastError((e as Error).message || '加载基本面画像失败');
      } finally {
        this.fundamentalsLoading = false;
      }
    },

    async rerunFundamentals(codes: string) {
      if (!codes.trim()) {
        toastError('请输入至少一个代码（逗号分隔）');
        return;
      }
      try {
        await api.postPreOpenFundamentalsRun({
          codes,
          market: this.market,
          date: this.date || undefined,
        });
        toastSuccess(`已重算 ${codes.split(',').filter(Boolean).length} 只标的的基本面画像`);
        await this.loadFundamentals(1);
      } catch (e) {
        toastError((e as Error).message || '重算失败');
      }
    },

    async exportPreOpenReport(format: 'md' | 'json') {
      try {
        const payload = await api.exportPreOpenReport(format, {
          market: this.market,
          date: this.date || undefined,
        });
        triggerBlobDownload(payload.blob, payload.filename);
      } catch {
        /* 已 toast */
      }
    },

    /* ============================================================
     * 盘后
     * ============================================================ */
    async loadPostCloseReport(force = false) {
      this.postReportLoading = true;
      this.postReportError = '';
      try {
        if (force) {
          const env = await api.postPostCloseRun({
            market: this.market,
            date: this.date || undefined,
            force: true,
          });
          this.postReport = env.data;
        } else {
          const env = await api.getPostCloseReport({ market: this.market, date: this.date || undefined });
          this.postReport = env.data;
        }
      } catch (e) {
        this.postReport = null;
        this.postReportError = (e as Error).message || '获取盘后报告失败';
      } finally {
        this.postReportLoading = false;
      }
    },

    async loadLimit(page?: number, pageSize?: number) {
      if (page !== undefined) this.limitPage = page;
      if (pageSize !== undefined) this.limitPageSize = pageSize;
      this.limitLoading = true;
      try {
        const env = await api.getPostCloseLimit({
          market: this.market,
          date: this.date || undefined,
          limit_type: this.limitType || undefined,
          sector: this.limitSector || undefined,
          only_up: this.limitSide === 'all' ? '' : this.limitSide === 'up' ? 'true' : 'false',
          page: this.limitPage,
          pageSize: this.limitPageSize,
        });
        this.limit = env.data;
      } catch (e) {
        toastError((e as Error).message || '加载涨跌停列表失败');
      } finally {
        this.limitLoading = false;
      }
    },

    async loadTechnical(page?: number, pageSize?: number) {
      if (page !== undefined) this.technicalPage = page;
      if (pageSize !== undefined) this.technicalPageSize = pageSize;
      this.technicalLoading = true;
      try {
        const env = await api.getPostCloseTechnical({
          market: this.market,
          codes: this.technicalCodes || undefined,
          page: this.technicalPage,
          pageSize: this.technicalPageSize,
        });
        this.technical = env.data;
      } catch (e) {
        toastError((e as Error).message || '加载技术扫描失败');
      } finally {
        this.technicalLoading = false;
      }
    },

    async exportPostCloseReport(format: 'md' | 'json') {
      try {
        const payload = await api.exportPostCloseReport(format, {
          market: this.market,
          date: this.date || undefined,
        });
        triggerBlobDownload(payload.blob, payload.filename);
      } catch {
        /* 已 toast */
      }
    },
  },
});
