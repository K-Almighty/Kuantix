/** data 路由 DTO（契约 §3.2） */

export interface SyncProgress {
  total: number;
  done: number;
  failed: number;
  quarantined: number;
  current: string;
  /** 完成百分比 0–100 */
  percent: number;
  started_at: string;
  updated_at: string;
}

export type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

/** 长任务 Job 模型（契约 §1.9；v1.2 增补 backtest 模块） */
export interface Job {
  job_id: string;
  module: 'data' | 'factor' | 'screen' | 'backtest';
  action: string;
  status: JobStatus;
  market: string;
  progress: SyncProgress | null;
  result_summary: Record<string, unknown> | null;
  error: { code: number; message: string } | null;
  created_at: string;
  updated_at: string;
}

export interface Coverage {
  securities: number;
  files: number;
  bars: number;
  disk_bytes: number;
  first_date: string;
  last_date: string;
}

/** 存储摘要（D1 新增，v1.5）：SQLite 主存储 + vipdoc 镜像的合并统计。
 * source 四态：empty / mirror_only / sqlite / both（前端据此分三场景引导）。 */
export type DataLakeSource = 'empty' | 'mirror_only' | 'sqlite' | 'both';

export interface DataLakeStorage {
  /** 兼容旧字段：market.db 路径 */
  db_path: string;
  /** 兼容旧字段：主存储后端 */
  backend: string;
  /** 兼容旧字段：securities 表条数 */
  securities: number;
  /** 兼容旧字段：daily_bars 行数 */
  daily_bars: number;
  sync_checkpoint: number;
  sync_meta: number;
  /** SQLite daily_bars 行数（新增） */
  sqlite_bars: number;
  /** SQLite securities 表条数（新增） */
  sqlite_securities: number;
  /** SQLite daily_bars 去重代码数（新增，securities 表可能为空） */
  sqlite_codes: number;
  /** vipdoc 镜像 .day 文件数（新增） */
  mirror_files: number;
  /** vipdoc 镜像磁盘字节数（新增） */
  mirror_disk_bytes: number;
  /** 存储状态四态（新增）：empty/mirror_only/sqlite/both */
  source: DataLakeSource;
}

export interface DataLakeStatus {
  market: string;
  data_date: string | null;
  coverage: Coverage;
  quarantine_count: number;
  latest_job: Job | null;
  in_sync_window: boolean;
  /** 存储摘要（D1 新增；旧后端/mock 可能缺失，判定时需回退 coverage） */
  storage?: DataLakeStorage;
  /** 二进制镜像是否启用（D1 新增） */
  vipdoc_mirror?: boolean;
}

export interface VerifyReport {
  market: string;
  coverage: Coverage;
  missing_days: string[];
  corrupt: string[];
  quarantined: QuarantineEntry[];
  excluded_count: number;
  generated_at: string;
}

export interface QuarantineEntry {
  code: string;
  market: string;
  /** 机器可读原因：unknown_security_type / readback_mismatch / fetch_failed / uint32_overflow / data_integrity / other */
  reason: string;
  detail: string;
  occurred_at: string;
  last_try: string;
  attempts: number;
}

export interface SyncRequest {
  mode: 'full' | 'incremental';
  market: string;
  years?: number;
  workers?: number;
}

export interface VerifyQuarantineRemoveResult {
  removed: string;
  reason?: string;
}

/** 证券搜索命中（契约 §3.2 D8，v1.2 增量） */
export interface SecurityHit {
  code: string;
  name: string;
  exchange: string;
  market: string;
  security_type: string;
}

/** D8 搜索响应载荷 */
export interface SecuritySearchResult {
  items: SecurityHit[];
  count: number;
}
