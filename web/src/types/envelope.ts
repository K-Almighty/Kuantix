/**
 * 统一信封（契约 §1.2 NF-9）。
 * 所有 JSON 响应（含错误响应、WebSocket 帧）均为 { code, message, data, meta }。
 */

export interface Meta {
  /** ISO-8601 秒级带时区 */
  generated_at: string;
  /** YYYY-MM-DD；无基准为 null */
  data_date: string | null;
  /** 市场码 CN/HK/US */
  market: string;
  /** 服务端处理耗时（毫秒） */
  elapsed_ms: number;
  /** Kuantix 版本 */
  version: string;
}

export interface Envelope<T = unknown> {
  /** 0=成功；其余见错误码表（§1.3） */
  code: number;
  message: string;
  data: T;
  meta: Meta;
}

/** 分页壳（§1.6） */
export interface Page<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
}

/** 错误信封 data 形态（fail-loud 类） */
export interface ErrorDetail {
  error_type?: string;
  path?: string;
  errors?: unknown[];
}
