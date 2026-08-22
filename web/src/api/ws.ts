/**
 * WebSocket 客户端（契约 §2.4.1 M17）。
 * - 每条帧都是合法 NF-9 信封，前端复用统一解析器。
 * - 断线指数退避重连：1s/2s/4s…上限 30s；重连后服务端回放 hello(+snapshot)。
 * - 客户端心跳 ping（每 25s）；服务端亦每 30s 主动 ping 保活。
 */
import type { WsEnvelope } from '../types/monitor';
import type { MonitorFeed, WsConnectionStatus } from './types';
import { API_BASE } from './config';

function buildWsUrl(market: string): string {
  const base = API_BASE.replace(/^http/, 'ws');
  return `${base}/monitor/ws?market=${encodeURIComponent(market)}`;
}

/** 客户端 ping 帧：按契约 §1.2，WS 帧也是合法信封（data.type=ping） */
function buildPingFrame(market: string): string {
  return JSON.stringify({
    code: 0,
    message: 'ok',
    data: { type: 'ping' },
    meta: {
      generated_at: new Date().toISOString(),
      data_date: null,
      market,
      elapsed_ms: 0,
      version: '0.1.0',
    },
  });
}

export class RealMonitorFeed implements MonitorFeed {
  private ws: WebSocket | null = null;
  private readonly url: string;
  private readonly market: string;
  private reconnectTimer: number | null = null;
  private pingTimer: number | null = null;
  private reconnectDelay = 1000;
  private userClosed = false;
  private messageHandler: ((frame: WsEnvelope) => void) | null = null;
  private statusHandler: ((status: WsConnectionStatus, detail?: string) => void) | null = null;

  constructor(market = 'CN') {
    this.market = market;
    this.url = buildWsUrl(market);
  }

  connect(): void {
    this.userClosed = false;
    this.open();
  }

  private open(): void {
    this.statusHandler?.('connecting');
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url);
    } catch (e) {
      this.scheduleReconnect((e as Error).message || 'WebSocket 构造失败');
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.reconnectDelay = 1000;
      this.statusHandler?.('open');
      this.startPing();
    };
    ws.onmessage = (ev: MessageEvent) => {
      this.handleRaw(ev.data);
    };
    ws.onerror = () => {
      // 状态变化由 onclose 统一上报
    };
    ws.onclose = (ev: CloseEvent) => {
      this.stopPing();
      if (!this.userClosed) {
        this.scheduleReconnect(`code=${ev.code}`);
      } else {
        this.statusHandler?.('closed');
      }
    };
  }

  private handleRaw(raw: unknown): void {
    let frame: WsEnvelope;
    try {
      frame = JSON.parse(String(raw)) as WsEnvelope;
    } catch {
      console.warn('[Kuantix WS] 非 JSON 帧，忽略', raw);
      return;
    }
    this.messageHandler?.(frame);
  }

  sendPing(): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(buildPingFrame(this.market));
    }
  }

  private startPing(): void {
    this.stopPing();
    this.pingTimer = window.setInterval(() => this.sendPing(), 25000);
  }

  private stopPing(): void {
    if (this.pingTimer !== null) {
      window.clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  private scheduleReconnect(detail: string): void {
    this.statusHandler?.('reconnecting', detail);
    if (this.reconnectTimer !== null) return;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, this.reconnectDelay);
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30000);
  }

  close(): void {
    this.userClosed = true;
    this.stopPing();
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.onmessage = null;
      this.ws.close();
      this.ws = null;
    }
    this.statusHandler?.('closed');
  }

  onMessage(handler: (frame: WsEnvelope) => void): void {
    this.messageHandler = handler;
  }

  onStatusChange(handler: (status: WsConnectionStatus, detail?: string) => void): void {
    this.statusHandler = handler;
  }
}
