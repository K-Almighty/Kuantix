/**
 * 监控看板 store（契约 §5.3）：状态灯/自选/持仓/规则/告警历史 + WS 实时流（M17）。
 * 轮询 5s（与 poll_interval 对齐）；WS 常驻 + 指数退避重连。
 */
import { defineStore } from 'pinia';
import { api, connectMonitorFeed } from '../api';
import type { MonitorFeed, WsConnectionStatus } from '../api/types';
import type {
  Alert,
  AlertLevel,
  ChannelInfo,
  CriterionInfo,
  MonitorStatus,
  Page,
  PresetStatus,
  PositionInput,
  PositionView,
  Rule,
  RuleInput,
  WatchlistAddResult,
  WatchlistItem,
  WsEnvelope,
} from '../types';
import { toastError, toastWarning } from '../utils/toast';

interface MonitorState {
  status: MonitorStatus | null;
  statusError: string;
  watchlist: Page<WatchlistItem> | null;
  positions: Page<PositionView> | null;
  rules: Page<Rule> | null;
  criteria: CriterionInfo[];
  channels: ChannelInfo[];
  presets: PresetStatus[] | null;
  presetsLoading: boolean;
  alerts: Page<Alert> | null;
  alertsLevel: string;
  /** WS 实时流（最近 100 条，snapshot 回放 + alert 增量） */
  liveAlerts: Alert[];
  wsStatus: WsConnectionStatus;
  wsDetail: string;
  feed: MonitorFeed | null;
  pingTimer: number | null;
  pollTimer: number | null;
  /** P1-2 配套：监控页各列表当前页（state 常驻，刷新后保持页面） */
  watchlistPage: number;
  positionsPage: number;
  rulesPage: number;
  watchlistPageSize: number;
  positionsPageSize: number;
  rulesPageSize: number;
}

export const useMonitorStore = defineStore('monitor', {
  state: (): MonitorState => ({
    status: null,
    statusError: '',
    watchlist: null,
    positions: null,
    rules: null,
    criteria: [],
    channels: [],
    presets: null,
    presetsLoading: false,
    alerts: null,
    alertsLevel: '',
    liveAlerts: [],
    wsStatus: 'idle',
    wsDetail: '',
    feed: null,
    pingTimer: null,
    pollTimer: null,
    // P1-2：默认每页 100，与后端路由 page_size 上限对齐（后端 max=500）
    watchlistPage: 1,
    positionsPage: 1,
    rulesPage: 1,
    watchlistPageSize: 100,
    positionsPageSize: 100,
    rulesPageSize: 100,
  }),

  getters: {
    healthTone(state): 'green' | 'yellow' | 'red' {
      const n = state.status?.consecutive_errors ?? 0;
      if (n >= 3) return 'red';
      if (n > 0) return 'yellow';
      return 'green';
    },
  },

  actions: {
    async init(): Promise<void> {
      await Promise.all([
        this.loadStatus(),
        this.loadWatchlist(),
        this.loadPositions(),
        this.loadRules(),
        this.loadCriteria(),
        this.loadChannels(),
        this.loadPresets(),
        this.loadAlerts(),
      ]);
      this.startPolling();
      this.connectWs();
    },

    startPolling(): void {
      if (this.pollTimer !== null) return;
      this.pollTimer = window.setInterval(() => {
        void this.loadStatus();
        // P1-2：5s 轮询保持当前分页位置（不重置回第 1 页）
        void this.loadWatchlist(this.watchlistPage, this.watchlistPageSize);
        void this.loadPositions(this.positionsPage, this.positionsPageSize);
      }, 5000);
    },

    stopPolling(): void {
      if (this.pollTimer !== null) {
        window.clearInterval(this.pollTimer);
        this.pollTimer = null;
      }
    },

    /* -------- REST -------- */
    async loadStatus(): Promise<void> {
      try {
        const env = await api.getMonitorStatus('CN');
        this.status = env.data;
        this.statusError = '';
      } catch (e) {
        this.statusError = e instanceof Error ? e.message : String(e);
      }
    },

    async loadWatchlist(page = 1, pageSize = 100): Promise<void> {
      try {
        // P1-2：显式记录当前页，避免 Pagination 组件翻页后 5s 轮询被重置回第 1 页
        this.watchlistPage = page;
        this.watchlistPageSize = pageSize;
        const env = await api.getWatchlist('CN', page, pageSize);
        this.watchlist = env.data;
      } catch {
        // 静默
      }
    },

    async loadPositions(page = 1, pageSize = 100): Promise<void> {
      try {
        this.positionsPage = page;
        this.positionsPageSize = pageSize;
        const env = await api.getPositions('CN', page, pageSize);
        this.positions = env.data;
      } catch {
        // 静默
      }
    },

    async loadRules(page = 1, pageSize = 100): Promise<void> {
      try {
        this.rulesPage = page;
        this.rulesPageSize = pageSize;
        const env = await api.getRules('CN', page, pageSize);
        this.rules = env.data;
      } catch {
        // 静默
      }
    },

    async loadCriteria(): Promise<void> {
      try {
        const env = await api.getCriteria();
        this.criteria = env.data.items;
      } catch {
        // 静默
      }
    },

    async loadChannels(): Promise<void> {
      try {
        const env = await api.getChannels();
        this.channels = env.data.items;
      } catch {
        // 静默
      }
    },

    async loadPresets(): Promise<void> {
      try {
        const env = await api.getPresets();
        this.presets = env.data;
      } catch {
        // 静默：预设区加载失败不应阻断看板
      }
    },

    async togglePreset(key: string): Promise<void> {
      this.presetsLoading = true;
      try {
        await api.togglePreset(key);
        await this.loadPresets();
      } catch {
        // 错误由 client 统一 toast
      } finally {
        this.presetsLoading = false;
      }
    },

    async loadAlerts(page = 1, pageSize = 50, level?: string): Promise<void> {
      try {
        const env = await api.getAlerts({
          market: 'CN',
          level: ((level ?? this.alertsLevel) || undefined) as AlertLevel | undefined,
          page,
          pageSize,
        });
        this.alerts = env.data;
      } catch (e) {
        toastError(e instanceof Error ? e.message : String(e));
      }
    },

    async startMonitor(): Promise<void> {
      const env = await api.postMonitorStart('CN');
      this.status = env.data;
    },

    async stopMonitor(): Promise<void> {
      const env = await api.postMonitorStop('CN');
      this.status = env.data;
    },

    async addWatchlist(codes: string[]): Promise<WatchlistAddResult> {
      const env = await api.postWatchlist(codes, 'CN', 'manual');
      await this.loadWatchlist();
      return env.data;
    },

    async removeWatchlist(code: string): Promise<void> {
      await api.deleteWatchlist(code, 'CN');
      await this.loadWatchlist();
    },

    async addPosition(input: PositionInput): Promise<PositionView> {
      const env = await api.postPosition(input);
      await this.loadPositions();
      return env.data;
    },

    async removePosition(code: string): Promise<void> {
      await api.deletePosition(code, 'CN');
      await this.loadPositions();
    },

    async addRule(input: RuleInput): Promise<Rule> {
      const env = await api.postRule(input);
      await this.loadRules();
      return env.data;
    },

    async setRuleEnabled(id: string, enabled: boolean): Promise<void> {
      await api.putRule(id, { enabled });
      await this.loadRules();
    },

    async removeRule(id: string): Promise<void> {
      await api.deleteRule(id);
      await this.loadRules();
    },

    /* -------- WebSocket（契约 §2.4.1 M17） -------- */
    connectWs(): void {
      if (this.feed) return;
      const feed = connectMonitorFeed('CN');
      this.feed = feed;
      feed.onStatusChange((status, detail) => {
        this.wsStatus = status;
        this.wsDetail = detail ?? '';
      });
      feed.onMessage((frame) => this.handleWsFrame(frame));
      feed.connect();
      this.pingTimer = window.setInterval(() => {
        if (this.wsStatus === 'open') this.feed?.sendPing();
      }, 25000);
    },

    disconnectWs(): void {
      if (this.pingTimer !== null) {
        window.clearInterval(this.pingTimer);
        this.pingTimer = null;
      }
      this.feed?.close();
      this.feed = null;
      this.wsStatus = 'closed';
    },

    reconnectWs(): void {
      this.disconnectWs();
      this.connectWs();
    },

    handleWsFrame(frame: WsEnvelope): void {
      if (frame.code !== 0) {
        toastError(`WS 错误：${frame.message}`);
        return;
      }
      const type = frame.data?.type;
      if (type === 'hello') {
        this.wsStatus = 'open';
      } else if (type === 'snapshot') {
        const alerts = frame.data.alerts as Alert[] | undefined;
        if (Array.isArray(alerts)) this.liveAlerts = alerts.slice(-100);
      } else if (type === 'alert') {
        const alert = frame.data.alert as Alert | undefined;
        if (alert) {
          this.liveAlerts.push(alert);
          if (this.liveAlerts.length > 100) this.liveAlerts.shift();
        }
      } else if (type === 'pong') {
        // 心跳保活确认
      } else if (type === 'bye') {
        const reason = (frame.data.reason as string | undefined) ?? '未知原因';
        toastWarning(`连接被服务端关闭：${reason}`);
      } else {
        // 未知帧类型：fail-loud（§6.1），不静默忽略
        console.warn('[Kuantix WS] 未知帧类型', type, frame);
      }
    },
  },
});
