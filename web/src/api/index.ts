/**
 * API 门面（纯真接口模式）：固定使用 RestApi 对接真实后端（契约 v1.2）。
 * - 无 mock 适配器；所有页面/组件统一从这里取 api 实例。
 * - 后端 Base URL 由 VITE_API_BASE 注入（默认 http://127.0.0.1:8899/api/v1，见 config.ts）。
 */
import type { KuantixApi, MonitorFeed } from './types';
import { RestApi } from './client';

/** 全局唯一 API 实例：真实后端 REST（无 mock 分支） */
export const api: KuantixApi = new RestApi();

/** 建立监控实时流（M17 WS 真连接，含断线重连） */
export function connectMonitorFeed(market = 'CN'): MonitorFeed {
  return api.connectMonitorFeed(market);
}
