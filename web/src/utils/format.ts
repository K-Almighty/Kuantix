/**
 * 格式化工具。
 * 注意契约 §1.4/§8：比例字段一律小数比例（0.05=5%），展示时 ×100 加 %。
 */

export function isNil(v: unknown): v is null | undefined {
  return v === null || v === undefined;
}

export function fmtPct(ratio: number | null | undefined, digits = 2): string {
  if (isNil(ratio) || Number.isNaN(ratio)) return '-';
  return `${(ratio * 100).toFixed(digits)}%`;
}

export function fmtNumber(v: number | null | undefined, digits = 2): string {
  if (isNil(v) || Number.isNaN(v)) return '-';
  return v.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function fmtInt(v: number | null | undefined): string {
  if (isNil(v) || Number.isNaN(v)) return '-';
  return v.toLocaleString('zh-CN');
}

export function fmtMoney(v: number | null | undefined, digits = 2): string {
  if (isNil(v) || Number.isNaN(v)) return '-';
  return `¥${v.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function fmtSignedPct(v: number | null | undefined, digits = 2): string {
  const s = fmtPct(v, digits);
  if (s === '-') return s;
  return v! > 0 ? `+${s}` : s;
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '-';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('zh-CN', { hour12: false });
}

/** date 语义字段：YYYY-MM-DD，不得当时间戳解析（契约 §1.7） */
export function fmtDate(date: string | null | undefined): string {
  if (!date) return '-';
  return date;
}

/** 大数值人性化：≥1亿 → x.xx 亿，≥1万 → x.xx 万（成交量/成交额展示） */
export function fmtBig(v: number | null | undefined, digits = 2): string {
  if (isNil(v) || Number.isNaN(v)) return '-';
  const abs = Math.abs(v);
  if (abs >= 1e8) return `${(v / 1e8).toFixed(digits)} 亿`;
  if (abs >= 1e4) return `${(v / 1e4).toFixed(digits)} 万`;
  return v.toLocaleString('zh-CN');
}

export function fmtBytes(n: number | null | undefined): string {
  if (isNil(n) || Number.isNaN(n)) return '-';
  const gb = n / 1024 / 1024 / 1024;
  if (gb >= 1) return `${gb.toFixed(2)} GB`;
  const mb = n / 1024 / 1024;
  return `${mb.toFixed(1)} MB`;
}

export function fmtSeconds(v: number | null | undefined): string {
  if (isNil(v) || Number.isNaN(v)) return '-';
  if (v < 60) return `${v.toFixed(1)}s`;
  return `${Math.floor(v / 60)}m${Math.round(v % 60)}s`;
}
