/**
 * 全局应用 store：市场切换（NF-6）、后端连接状态（/api/version + /health 探测）、
 * 数据湖状态指示（§5.1：30s 轮询 + 同步 Job 1.5s 轮询）。
 */
import { defineStore } from 'pinia';
import { api } from '../api';
import type { DataLakeSource, DataLakeStatus, HealthInfo, Job, SyncRequest, VersionInfo } from '../types';
import { API_BASE } from '../api/config';

interface AppState {
  market: string;
  version: VersionInfo | null;
  health: HealthInfo | null;
  connected: boolean;
  probing: boolean;
  dataStatus: DataLakeStatus | null;
  dataStatusError: string;
  dataStatusLoading: boolean;
  syncJob: Job | null;
  syncPolling: boolean;
  statusTimer: number | null;
  syncTimer: number | null;
}

export const useAppStore = defineStore('app', {
  state: (): AppState => ({
    market: 'CN',
    version: null,
    health: null,
    connected: false,
    probing: false,
    dataStatus: null,
    dataStatusError: '',
    dataStatusLoading: false,
    syncJob: null,
    syncPolling: false,
    statusTimer: null,
    syncTimer: null,
  }),

  getters: {
    /** 市场启用状态：P0 仅 CN；HK/US 置灰（契约 §1.8） */
    marketsEnabled(state): Record<string, boolean> {
      const he = state.health?.markets_enabled;
      if (!he) return { CN: true, HK: false, US: false };
      if (Array.isArray(he)) {
        const rec: Record<string, boolean> = { CN: true, HK: false, US: false };
        he.forEach((m) => {
          rec[m] = true;
        });
        return rec;
      }
      return he;
    },
    latestDataDate(state): string {
      return state.dataStatus?.data_date ?? '-';
    },
    inSyncWindow(state): boolean {
      return state.dataStatus?.in_sync_window ?? false;
    },
    /** 数据湖存储状态：D1 storage.source 四态（empty/mirror_only/sqlite/both）。
     * 旧后端/mock 无 storage.source 时按 coverage 回退推导：securities===0 → empty，
     * 否则视为 both（有数据即不误判空湖）。dataStatus 为 null（未加载/后端未连接）
     * 返回 'unknown'，避免与连接错误混淆。 */
    dataLakeSource(state): DataLakeSource | 'unknown' {
      const source = state.dataStatus?.storage?.source;
      if (source) return source;
      if (state.dataStatus === null) return 'unknown';
      return (state.dataStatus.coverage?.securities ?? 0) === 0 ? 'empty' : 'both';
    },
    /** 数据湖空态（场景 A：都空，真未建湖）：仅 storage.source==='empty' 才算空，
     * 避免"仅镜像有（未迁移）"被误判为空湖而引导重拉 508M。 */
    dataLakeEmpty(state): boolean {
      if (state.dataStatus === null) return false;
      const source = state.dataStatus.storage?.source;
      if (source) return source === 'empty';
      // 旧后端/mock 兼容：无 storage.source 时退回 coverage 判定
      return (state.dataStatus.coverage?.securities ?? 0) === 0;
    },
    /** 数据湖"仅镜像有 / 未迁移"（场景 B）：storage.source==='mirror_only'，
     * 应引导 data migrate 而不是重新 data sync。 */
    dataLakeMirrorOnly(state): boolean {
      return this.dataLakeSource === 'mirror_only';
    },
    apiBase(): string {
      return API_BASE;
    },
  },

  actions: {
    async init(): Promise<void> {
      await this.probe();
      this.startStatusPolling();
    },

    async probe(): Promise<void> {
      this.probing = true;
      try {
        const v = await api.getVersion();
        const h = await api.getHealth();
        this.version = v.data;
        this.health = h.data;
        this.connected = true;
      } catch {
        this.connected = false;
      } finally {
        this.probing = false;
      }
    },

    setMarket(market: string): void {
      this.market = market;
    },

    startStatusPolling(): void {
      if (this.statusTimer !== null) return;
      void this.refreshDataStatus();
      this.statusTimer = window.setInterval(() => {
        void this.refreshDataStatus();
      }, 30000);
    },

    stopStatusPolling(): void {
      if (this.statusTimer !== null) {
        window.clearInterval(this.statusTimer);
        this.statusTimer = null;
      }
    },

    async refreshDataStatus(): Promise<void> {
      if (this.dataStatusLoading) return;
      this.dataStatusLoading = true;
      try {
        const env = await api.getDataStatus(this.market);
        this.dataStatus = env.data;
        this.dataStatusError = '';
      } catch (e) {
        this.dataStatusError = e instanceof Error ? e.message : String(e);
      } finally {
        this.dataStatusLoading = false;
      }
    },

    async startSync(mode: 'full' | 'incremental'): Promise<void> {
      const req: SyncRequest = { mode, market: this.market };
      if (mode === 'full') {
        req.years = 10;
        req.workers = 4;
      }
      const env = await api.postDataSync(req);
      this.syncJob = env.data;
      this.startSyncPolling();
    },

    async cancelSync(): Promise<void> {
      if (!this.syncJob) return;
      const env = await api.postDataSyncCancel(this.syncJob.job_id);
      this.syncJob = env.data;
    },

    startSyncPolling(): void {
      if (this.syncPolling) return;
      this.syncPolling = true;
      const tick = async (): Promise<void> => {
        if (!this.syncJob) {
          this.stopSyncPolling();
          return;
        }
        try {
          const env = await api.getDataSyncJob(this.syncJob.job_id);
          this.syncJob = env.data;
          if (env.data.status !== 'queued' && env.data.status !== 'running') {
            this.stopSyncPolling();
            void this.refreshDataStatus();
          }
        } catch {
          this.stopSyncPolling();
        }
      };
      void tick();
      this.syncTimer = window.setInterval(() => {
        void tick();
      }, 1500);
    },

    stopSyncPolling(): void {
      if (this.syncTimer !== null) {
        window.clearInterval(this.syncTimer);
        this.syncTimer = null;
      }
      this.syncPolling = false;
    },
  },
});
