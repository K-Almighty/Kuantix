<script setup lang="ts">
/**
 * 回测页 /backtest（契约 §3.6 B1–B4，v1.2 增量）
 * 左配置面板：标的池（D8 搜索 + 已选列表）/ 策略（下拉 + 参数表单）/ 时间 / 资金与成本
 * 右报告面板：组合净值曲线 + 回撤、指标卡、逐标的净值 + 成交明细、导出 JSON。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { EChartsCoreOption } from 'echarts/core';
import { useAppStore } from '../stores/app';
import { useBacktestStore } from '../stores/backtest';
import { api } from '../api';
import type { ExportPayload } from '../api/types';
import type {
  BacktestParamSchema,
  BacktestResult,
  BacktestRunRequest,
  BacktestStrategySchema,
  KlineWithSignals,
  SignalPoint,
} from '../types';
import type { SecurityHit } from '../types/data';
import { gradePerformance } from '../grading';
import { envelopeToBlob } from '../utils/download';
import { fmtInt, fmtNumber, fmtPct, fmtSignedPct } from '../utils/format';
import { toastError, toastSuccess, toastWarning } from '../utils/toast';
import EChart from '../components/EChart.vue';
import ExportButton from '../components/ExportButton.vue';
import GradeBadge from '../components/GradeBadge.vue';
import GradeDetails from '../components/GradeDetails.vue';
import JobProgress from '../components/JobProgress.vue';
import KlineChart from '../components/KlineChart.vue';
import SecuritySearchBox from '../components/SecuritySearchBox.vue';
import StateBlock from '../components/StateBlock.vue';

const app = useAppStore();
const store = useBacktestStore();

/* ---------- 标的池 ---------- */
const codes = ref<string[]>([]);

function onSearchSelect(hit: SecurityHit): void {
  if (!codes.value.includes(hit.code)) codes.value.push(hit.code);
}

function removeCode(code: string): void {
  const idx = codes.value.indexOf(code);
  if (idx >= 0) codes.value.splice(idx, 1);
}

/* ---------- 策略 + 参数 ---------- */
const strategy = ref('ma_cross');
const paramValues = ref<Record<string, string | number | boolean>>({});

const selectedStrategy = computed<BacktestStrategySchema | undefined>(() =>
  store.strategies.find((s) => s.name === strategy.value),
);

function onStrategyChange(): void {
  // 重置参数为 schema 默认值
  const next: Record<string, string | number | boolean> = {};
  for (const p of selectedStrategy.value?.params ?? []) {
    const d = p.default;
    next[p.name] = d === null || d === undefined ? '' : (d as string | number | boolean);
  }
  paramValues.value = next;
}

function paramInputType(p: BacktestParamSchema): string {
  if (p.type === 'int' || p.type === 'float') return 'number';
  if (p.type === 'bool') return 'checkbox';
  return 'text';
}

function parseParam(p: BacktestParamSchema, raw: string | number | boolean): string | number | boolean {
  if (p.type === 'bool') return Boolean(raw);
  if (p.type === 'int') return Math.round(Number(raw));
  if (p.type === 'float') return Number(raw);
  return String(raw);
}

function onParamInput(p: BacktestParamSchema, event: Event): void {
  const el = event.target as HTMLInputElement;
  const value: string | number | boolean =
    p.type === 'bool' ? el.checked : el.value;
  paramValues.value[p.name] = parseParam(p, value);
}

/* ---------- 时间 / 资金 ---------- */
const startDate = ref('2020-01-01');
const endDate = ref('2025-12-31');
const cash = ref(1000000);
const commission = ref(0.0003);
const minCommission = ref(5.0);
const stampTax = ref(0.001);
const slippage = ref(0);
const execution = ref<'next_open' | 'next_close'>('next_open');

const EXECUTIONS: { value: 'next_open' | 'next_close'; label: string }[] = [
  { value: 'next_open', label: '开盘价' },
  { value: 'next_close', label: '收盘价' },
];

/* ---------- 运行 ---------- */
function validate(): string | null {
  if (codes.value.length === 0) return '请先选择至少一个标的（搜索代码/名称添加）';
  if (!strategy.value) return '请选择策略';
  if (!startDate.value || !endDate.value) return '请选择起止日期';
  if (startDate.value > endDate.value) return '起始日期不能晚于结束日期';
  if (!(cash.value > 0)) return '初始资金必须为正数';
  return null;
}

async function onRun(): Promise<void> {
  const invalid = validate();
  if (invalid) {
    toastWarning(invalid);
    return;
  }
  const req: BacktestRunRequest = {
    market: app.market,
    codes: [...codes.value],
    strategy: strategy.value,
    params: { ...paramValues.value },
    start: startDate.value,
    end: endDate.value,
    cash: Number(cash.value),
    commission: Number(commission.value),
    min_commission: Number(minCommission.value),
    stamp_tax: Number(stampTax.value),
    slippage: Number(slippage.value),
    execution: execution.value,
  };
  try {
    await store.run(req);
    toastSuccess(`回测已提交：${req.strategy} · ${req.codes.length} 只标的`);
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

/* ---------- 结果渲染 ---------- */
const perfKeys: { key: string; label: string; fmt: (v: number | null) => string }[] = [
  { key: 'total_return', label: '总收益率', fmt: (v) => fmtSignedPct(v) },
  { key: 'annual_return', label: '年化收益', fmt: (v) => fmtSignedPct(v) },
  { key: 'max_drawdown', label: '最大回撤', fmt: (v) => fmtPct(v) },
  { key: 'sharpe', label: '夏普比率', fmt: (v) => fmtNumber(v) },
  { key: 'win_rate', label: '胜率', fmt: (v) => fmtPct(v) },
  { key: 'total_trades', label: '成交笔数', fmt: (v) => fmtInt(v) },
  { key: 'profit_factor', label: '盈亏比', fmt: (v) => fmtNumber(v) },
  { key: 'volatility', label: '年化波动', fmt: (v) => fmtPct(v) },
];

/** 19 项绩效指标表（B-8：B4 per_code.performance 已含全部字段，前端完整展示） */
const PERF_19_KEYS: { key: string; label: string; group: string; fmt: (v: number | null) => string }[] = [
  { key: 'total_return', label: '总收益率', group: '收益', fmt: (v) => fmtSignedPct(v) },
  { key: 'annual_return', label: '年化收益', group: '收益', fmt: (v) => fmtSignedPct(v) },
  { key: 'max_drawdown', label: '最大回撤', group: '风险', fmt: (v) => fmtPct(v) },
  { key: 'max_dd_duration', label: '回撤持续天数', group: '风险', fmt: (v) => fmtInt(v) },
  { key: 'sharpe', label: '夏普比率', group: '风险调整', fmt: (v) => fmtNumber(v) },
  { key: 'sortino', label: '索提诺比率', group: '风险调整', fmt: (v) => fmtNumber(v) },
  { key: 'calmar', label: '卡玛比率', group: '风险调整', fmt: (v) => fmtNumber(v) },
  { key: 'volatility', label: '年化波动率', group: '风险', fmt: (v) => fmtPct(v) },
  { key: 'total_trades', label: '成交笔数', group: '交易', fmt: (v) => fmtInt(v) },
  { key: 'win_trades', label: '盈利笔数', group: '交易', fmt: (v) => fmtInt(v) },
  { key: 'lose_trades', label: '亏损笔数', group: '交易', fmt: (v) => fmtInt(v) },
  { key: 'rejected_trades', label: '拒绝笔数', group: '交易', fmt: (v) => fmtInt(v) },
  { key: 'win_rate', label: '胜率', group: '交易', fmt: (v) => fmtPct(v) },
  { key: 'profit_factor', label: '利润因子', group: '交易', fmt: (v) => fmtNumber(v) },
  { key: 'avg_win', label: '平均盈利', group: '交易', fmt: (v) => fmtSignedPct(v) },
  { key: 'avg_loss', label: '平均亏损', group: '交易', fmt: (v) => fmtSignedPct(v) },
  { key: 'max_win', label: '单笔最大盈利', group: '交易', fmt: (v) => fmtPct(v) },
  { key: 'max_loss', label: '单笔最大亏损', group: '交易', fmt: (v) => fmtPct(v) },
  { key: 'avg_holding_days', label: '平均持有天数', group: '交易', fmt: (v) => fmtNumber(v) },
];

/* ---------- 单代码下钻（B-5：K线 + 买卖点 + 评级 + 19 项绩效） ---------- */
const drillCode = ref('');
const kline = ref<KlineWithSignals | null>(null);
const klineLoading = ref(false);
const klineError = ref('');

const drillPerf = computed(() => {
  const result = store.result;
  if (!result || !drillCode.value) return null;
  return result.per_code[drillCode.value]?.performance ?? null;
});

const drillGrade = computed(() => (drillPerf.value ? gradePerformance(drillPerf.value) : null));

/** 单标的评级（供逐标的绩效表评级列展示） */
function perCodeGrade(code: string) {
  const perf = store.result?.per_code[code]?.performance ?? null;
  return perf ? gradePerformance(perf) : null;
}

/** 归一化成交日期：'20200102' → '2020-01-02'（兼容已含 '-' 的日期） */
function normTradeDate(dt: number | string): string {
  const s = String(dt).slice(0, 10);
  if (/^\d{8}$/.test(s)) return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
  return s;
}

/** 买卖点：优先 KlineWithSignals.buy_points/sell_points；缺失时从 B4 trades direction 推导 */
const drillBuyPoints = computed<SignalPoint[]>(() => {
  if (kline.value?.buy_points && kline.value.buy_points.length > 0) return kline.value.buy_points;
  return deriveTradePoints('BUY');
});

const drillSellPoints = computed<SignalPoint[]>(() => {
  if (kline.value?.sell_points && kline.value.sell_points.length > 0) return kline.value.sell_points;
  return deriveTradePoints('SELL');
});

function deriveTradePoints(direction: string): SignalPoint[] {
  const result = store.result;
  if (!result || !drillCode.value) return [];
  const trades = result.per_code[drillCode.value]?.trades ?? [];
  return trades
    .filter((t) => !t.rejected && String(t.direction).toUpperCase().includes(direction))
    .map((t) => ({ date: normTradeDate(t.datetime), price: t.price }))
    .filter((p) => p.date.length >= 10);
}

async function openDrill(code: string): Promise<void> {
  drillCode.value = code;
  kline.value = null;
  klineError.value = '';
  klineLoading.value = true;
  // 传当前回测任务的区间/策略（契约 §3.8 B5 query：market/start/end/strategy，后端有默认值）
  const result = store.result;
  try {
    const env = await api.getKline(code, {
      market: app.market,
      start: result?.start_date ?? undefined,
      end: result?.end_date ?? undefined,
      strategy: result?.strategy ?? undefined,
    });
    kline.value = env.data;
  } catch (e) {
    klineError.value = e instanceof Error ? e.message : String(e);
  } finally {
    klineLoading.value = false;
  }
}

function closeDrill(): void {
  drillCode.value = '';
  kline.value = null;
  klineError.value = '';
}

// 下钻弹窗打开时若有未完成 K 线请求，关闭时不做额外处理（组件卸载即止）
watch(drillCode, (v) => {
  if (!v) kline.value = null;
});

const combinedEquityOption = computed<EChartsCoreOption>(() => {
  const curve = store.result?.combined.equity_curve ?? [];
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['净值', '回撤'] },
    grid: { left: 60, right: 24, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: curve.map((p) => p.datetime) },
    yAxis: [
      { type: 'value', name: '净值', scale: true },
      { type: 'value', name: '回撤', scale: true },
    ],
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
    series: [
      {
        name: '净值',
        type: 'line',
        showSymbol: false,
        data: curve.map((p) => p.total),
        lineStyle: { width: 2, color: '#2563eb' },
        areaStyle: { opacity: 0.08, color: '#2563eb' },
      },
      {
        name: '回撤',
        type: 'line',
        yAxisIndex: 1,
        showSymbol: false,
        data: curve.map((p) => p.drawdown_pct ?? p.drawdown),
        lineStyle: { width: 1, color: '#dc2626' },
      },
    ],
  };
});

/** 逐标的净值（归一化到 1.0）多序列：x 轴用所有日期并集 */
const perCodeEquityOption = computed<EChartsCoreOption>(() => {
  const result = store.result;
  if (!result) return {};
  const dates = new Set<string>();
  const normMap: Record<string, Map<string, number>> = {};
  for (const code of result.codes) {
    const curve = result.per_code[code]?.equity_curve ?? [];
    const base = curve.length > 0 && curve[0].total !== 0 ? curve[0].total : 1;
    const m = new Map<string, number>();
    for (const p of curve) {
      m.set(p.datetime, round(p.total / base));
      dates.add(p.datetime);
    }
    normMap[code] = m;
  }
  const ordered = Array.from(dates).sort();
  const series = result.codes.map((code) => ({
    name: code,
    type: 'line' as const,
    showSymbol: false,
    data: ordered.map((d) => normMap[code]?.get(d) ?? null),
    lineStyle: { width: 1.5 },
    connectNulls: true,
  }));
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: result.codes, type: 'scroll' },
    grid: { left: 60, right: 24, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: ordered },
    yAxis: { type: 'value', name: '归一化净值', scale: true },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
    series,
  };
});

function round(v: number, digits = 6): number {
  const p = 10 ** digits;
  return Math.round(v * p) / p;
}

/* ---------- 月度/年度收益（从 combined.equity_curve 派生，B-9 收益热力图） ---------- */
type EquityPoint = { datetime: string; total: number };

/** 按月聚合：取每月最后一个净值点作为月终点，返回 [年, 月, 月收益] 序列 */
const monthlyReturns = computed<{ ym: string; year: string; month: number; ret: number | null }[]>(() => {
  const curve = (store.result?.combined.equity_curve ?? []) as EquityPoint[];
  if (curve.length === 0) return [];
  const byMonth = new Map<string, { last: number; first: number }>();
  for (const p of curve) {
    const ym = String(p.datetime).slice(0, 7); // YYYY-MM
    const v = Number(p.total);
    if (!Number.isFinite(v)) continue;
    const cur = byMonth.get(ym);
    if (!cur) byMonth.set(ym, { last: v, first: v });
    else cur.last = v;
  }
  const months = Array.from(byMonth.keys()).sort();
  const out: { ym: string; year: string; month: number; ret: number | null }[] = [];
  let prevLast: number | null = null;
  for (const m of months) {
    const { last, first } = byMonth.get(m)!;
    let ret: number | null = null;
    if (prevLast !== null && prevLast > 0) ret = last / prevLast - 1;
    else if (first > 0) ret = last / first - 1; // 首月：月内收益
    out.push({ ym: m, year: m.slice(0, 4), month: Number(m.slice(5, 7)), ret });
    prevLast = last;
  }
  return out;
});

/** 年度收益：每年最后一个净值 / 上一年末净值 - 1 */
const annualReturns = computed<{ year: string; ret: number | null }[]>(() => {
  const curve = (store.result?.combined.equity_curve ?? []) as EquityPoint[];
  if (curve.length === 0) return [];
  const byYear = new Map<string, { last: number; first: number }>();
  for (const p of curve) {
    const y = String(p.datetime).slice(0, 4); // YYYY
    const v = Number(p.total);
    if (!Number.isFinite(v)) continue;
    const cur = byYear.get(y);
    if (!cur) byYear.set(y, { last: v, first: v });
    else cur.last = v;
  }
  const years = Array.from(byYear.keys()).sort();
  const out: { year: string; ret: number | null }[] = [];
  let prevLast: number | null = null;
  for (const y of years) {
    const { last, first } = byYear.get(y)!;
    let ret: number | null = null;
    if (prevLast !== null && prevLast > 0) ret = last / prevLast - 1;
    else if (first > 0) ret = last / first - 1;
    out.push({ year: y, ret });
    prevLast = last;
  }
  return out;
});

/** 月度收益热力图：行=年，列=1–12 月，值=月收益(%) */
const monthlyHeatmapOption = computed<EChartsCoreOption>(() => {
  const data = monthlyReturns.value;
  if (data.length === 0) return {};
  const years = Array.from(new Set(data.map((d) => d.year))).sort();
  const months = Array.from({ length: 12 }, (_, i) => i + 1);
  const pts: [number, number, number | null][] = [];
  const vals: number[] = [];
  for (const d of data) {
    if (d.ret === null) continue;
    const x = months.indexOf(d.month);
    const y = years.indexOf(d.year);
    const v = round(d.ret * 100, 2);
    pts.push([x, y, v]);
    vals.push(v);
  }
  const vmin = vals.length ? Math.min(...vals) : -1;
  const vmax = vals.length ? Math.max(...vals) : 1;
  return {
    tooltip: {
      position: 'top',
      formatter: (p: { value: [number, number, number | null] }) => {
        const [xi, yi, v] = p.value;
        return `${years[yi]}-${String(months[xi]).padStart(2, '0')}<br/>月收益 ${v === null ? '-' : v.toFixed(2) + '%'}`;
      },
    },
    grid: { left: 60, right: 20, top: 20, bottom: 60 },
    xAxis: { type: 'category', data: months.map((m) => `${m}月`), name: '月份', splitArea: { show: true } },
    yAxis: { type: 'category', data: years, name: '年份', splitArea: { show: true } },
    visualMap: {
      min: vmin,
      max: vmax,
      calculable: true,
      orient: 'horizontal',
      left: 'center',
      bottom: 4,
      inRange: { color: ['#16a34a', '#86efac', '#fef3c7', '#fca5a5', '#dc2626'] },
    },
    series: [
      {
        name: '月收益',
        type: 'heatmap',
        data: pts,
        label: {
          show: true,
          formatter: (p: { value: [number, number, number | null] }) =>
            p.value[2] === null ? '-' : `${p.value[2].toFixed(1)}%`,
        },
        itemStyle: { borderWidth: 1, borderColor: '#fff' },
        emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.4)' } },
      },
    ],
  };
});

/** 年度收益柱状图（红涨绿跌配色） */
const annualReturnOption = computed<EChartsCoreOption>(() => {
  const data = annualReturns.value.filter((d) => d.ret !== null);
  if (data.length === 0) return {};
  return {
    tooltip: { trigger: 'axis', formatter: (ps: { name: string; value: number }[]) => `${ps[0].name} 年收益 ${ps[0].value.toFixed(2)}%` },
    grid: { left: 60, right: 24, top: 24, bottom: 36 },
    xAxis: { type: 'category', data: data.map((d) => d.year) },
    yAxis: { type: 'value', name: '年收益', axisLabel: { formatter: (v: number) => `${v.toFixed(0)}%` } },
    series: [
      {
        type: 'bar',
        data: data.map((d) => ({
          value: round(d.ret! * 100, 2),
          itemStyle: { color: d.ret! >= 0 ? '#dc2626' : '#16a34a' },
        })),
        barWidth: '55%',
      },
    ],
  };
});

const selectedCode = ref('');

const selectedTrades = computed(() => {
  const result = store.result;
  if (!result || !selectedCode.value) return [];
  return result.per_code[selectedCode.value]?.trades ?? [];
});

function onSelectCode(code: string): void {
  selectedCode.value = selectedCode.value === code ? '' : code;
}

async function exportBacktest(): Promise<{ blob: Blob; filename: string }> {
  const result = store.result;
  if (!result) throw new Error('无回测结果可导出');
  return {
    blob: new Blob([JSON.stringify(result, null, 2)], { type: 'application/json; charset=utf-8' }),
    filename: `backtest_${result.strategy}_${result.codes.join('_')}.json`,
  };
}

/* 应用「参数寻优」页跳转带来的回测预填上下文（策略/参数/标的/区间）；
   返回是否应自动触发一次回测（「查看交易明细」直出）。 */
function applyPreset(): boolean {
  const preset = store.consumePreset();
  if (!preset) return false;
  // 清空上一次回测结果，避免「查看」后仍展示旧策略的交易明细（异常展示根源）
  store.clearResult();
  // 策略存在才切换；否则保留默认
  if (store.strategies.some((s) => s.name === preset.strategy)) {
    strategy.value = preset.strategy;
    // 参数按 schema 类型归一后填入
    const next: Record<string, string | number | boolean> = {};
    for (const p of selectedStrategy.value?.params ?? []) {
      const raw = preset.params[p.name];
      if (raw === undefined || raw === null) {
        next[p.name] = p.default ?? '';
      } else {
        next[p.name] = parseParam(p, String(raw));
      }
    }
    paramValues.value = next;
  }
  if (preset.codes?.length) codes.value = [...preset.codes];
  if (preset.startDate) startDate.value = preset.startDate;
  if (preset.endDate) endDate.value = preset.endDate;
  return !!preset.autoRun;
}

onMounted(() => {
  void store.loadStrategies().then(() => {
    onStrategyChange();
    // 参数寻优「查看」跳转：预填后自动跑一次回测，直接展示该策略交易明细
    const shouldAutoRun = applyPreset();
    if (shouldAutoRun) void onRun();
  });
});

onBeforeUnmount(() => {
  store.stopPolling();
});
</script>

<template>
  <div class="backtest-page">
    <!-- 左栏：配置 -->
    <aside class="config-panel panel">
      <section class="config-section">
        <h3>标的池（{{ codes.length }}）</h3>
        <SecuritySearchBox :market="app.market" placeholder="搜索代码/名称添加标的，如 600000 / 浦发" @select="onSearchSelect" />
        <div v-if="codes.length" class="code-chips">
          <span v-for="c in codes" :key="c" class="code-chip">
            {{ c }}
            <button type="button" class="chip-x" title="移除" @click="removeCode(c)">×</button>
          </span>
        </div>
        <p v-else class="hint">至少添加 1 只标的（最多 20 只）</p>
      </section>

      <section class="config-section">
        <h3>策略</h3>
        <StateBlock v-if="store.strategiesError && !app.dataLakeEmpty" state="error" :message="store.strategiesError" />
        <StateBlock v-else-if="store.strategiesError && app.dataLakeEmpty" state="empty" message="数据湖为空：请先同步行情数据（见顶部引导条），再加载策略" />
        <StateBlock v-else-if="store.strategies.length === 0" state="empty" message="策略列表为空：后端未返回任何预置策略，请刷新重试或检查 /api/v1/backtest/strategies" />
        <template v-else>
          <select v-model="strategy" class="input" @change="onStrategyChange">
            <option v-for="s in store.strategies" :key="s.name" :value="s.name">{{ s.label }}（{{ s.name }}）</option>
          </select>
          <p v-if="selectedStrategy" class="strategy-desc">{{ selectedStrategy.description }}</p>
          <div v-if="selectedStrategy?.params.length" class="param-grid">
            <label v-for="p in selectedStrategy.params" :key="p.name" class="param-field">
              <span class="param-label">{{ p.label || p.name }}</span>
              <input
                v-if="p.type === 'bool'"
                type="checkbox"
                :checked="Boolean(paramValues[p.name])"
                @change="onParamInput(p, $event)"
              />
              <input
                v-else
                class="input"
                :type="paramInputType(p)"
                :value="paramValues[p.name]"
                :min="p.min_value"
                :max="p.max_value"
                @input="onParamInput(p, $event)"
              />
            </label>
          </div>
        </template>
      </section>

      <section class="config-section">
        <h3>时间范围</h3>
        <div class="row">
          <label class="field"><span>起始</span><input v-model="startDate" class="input" type="date" /></label>
          <label class="field"><span>结束</span><input v-model="endDate" class="input" type="date" /></label>
        </div>
      </section>

      <section class="config-section">
        <h3>资金与成本</h3>
        <label class="field"><span>初始资金</span><input v-model.number="cash" class="input" type="number" min="1000" step="10000" /></label>
        <div class="row">
          <label class="field"><span>佣金率</span><input v-model.number="commission" class="input" type="number" min="0" step="0.0001" /></label>
          <label class="field"><span>最低佣金</span><input v-model.number="minCommission" class="input" type="number" min="0" step="0.1" /></label>
        </div>
        <div class="row">
          <label class="field"><span>印花税</span><input v-model.number="stampTax" class="input" type="number" min="0" step="0.0001" /></label>
          <label class="field"><span>滑点</span><input v-model.number="slippage" class="input" type="number" min="0" step="0.001" /></label>
        </div>
        <label class="field"><span>成交价</span>
          <select v-model="execution" class="input">
            <option v-for="e in EXECUTIONS" :key="e.value" :value="e.value">{{ e.label }}</option>
          </select>
        </label>
      </section>

      <button class="btn btn-primary run-btn" :disabled="store.running" @click="onRun">
        {{ store.running ? '回测运行中…' : '开始回测' }}
      </button>
    </aside>

    <!-- 右栏：报告 -->
    <main class="report-panel">
      <div v-if="store.resultError" class="error-banner">⚠ {{ store.resultError }}</div>
      <JobProgress v-if="store.job && store.job.status !== 'done' && store.job.status !== 'failed'" :job="store.job" />

      <div v-if="!store.result && !store.running && !store.resultError && !store.job" class="placeholder panel">
        <p v-if="app.dataLakeEmpty">数据湖为空：回测依赖本地行情数据，请先同步（见顶部引导条），再开始回测。</p>
        <p v-else>选择标的与策略后点击「开始回测」，结果将展示净值曲线 / 回撤 / 绩效指标 / 成交明细。</p>
      </div>

      <div v-if="store.result" class="report-content">
        <div class="result-toolbar">
          <span class="result-title">
            {{ store.result.strategy }} · {{ store.result.codes.length }} 只标的
            · {{ store.result.start_date }} ~ {{ store.result.end_date }}
          </span>
          <ExportButton :fetcher="exportBacktest" label="导出JSON" />
        </div>

        <section class="report-section panel">
          <h3>组合绩效指标</h3>
          <div class="metric-cards">
            <div v-for="k in perfKeys" :key="k.key" class="metric-card">
              <div class="metric-label">{{ k.label }}</div>
              <div class="metric-value">{{ k.fmt(store.result.combined.performance[k.key] ?? null) }}</div>
            </div>
          </div>
        </section>

        <section class="report-section panel">
          <h3>组合净值曲线与回撤</h3>
          <EChart :option="combinedEquityOption" height="340px" />
        </section>

        <section class="report-section panel">
          <h3>逐标的净值（归一化）</h3>
          <EChart :option="perCodeEquityOption" height="300px" />
        </section>

        <section class="report-section panel">
          <h3>月度收益热力图</h3>
          <EChart :option="monthlyHeatmapOption" height="340px" />
        </section>

        <section class="report-section panel">
          <h3>年度收益</h3>
          <EChart :option="annualReturnOption" height="300px" />
        </section>

        <section class="report-section panel">
          <h3>逐标的绩效</h3>
          <div class="table-wrap">
            <table class="tbl">
              <thead>
                <tr>
                  <th>代码</th>
                  <th>总收益</th>
                  <th>年化</th>
                  <th>最大回撤</th>
                  <th>夏普</th>
                  <th>胜率</th>
                  <th>成交</th>
                  <th>评级</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="code in store.result.codes" :key="code">
                  <td class="mono-cell">{{ code }}</td>
                  <td>{{ fmtSignedPct(store.result.per_code[code].performance.total_return ?? null) }}</td>
                  <td>{{ fmtSignedPct(store.result.per_code[code].performance.annual_return ?? null) }}</td>
                  <td>{{ fmtPct(store.result.per_code[code].performance.max_drawdown ?? null) }}</td>
                  <td>{{ fmtNumber(store.result.per_code[code].performance.sharpe ?? null) }}</td>
                  <td>{{ fmtPct(store.result.per_code[code].performance.win_rate ?? null) }}</td>
                  <td>{{ fmtInt(store.result.per_code[code].performance.total_trades ?? null) }}</td>
                  <td>
                    <GradeBadge :result="perCodeGrade(code)" size="sm" :show-score="false" />
                  </td>
                  <td>
                    <button class="btn btn-ghost btn-xs" @click="onSelectCode(code)">
                      {{ selectedCode === code ? '收起' : '成交' }}
                    </button>
                    <button class="btn btn-ghost btn-xs" @click="openDrill(code)">下钻</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="selectedCode" class="trades-block">
            <h4>{{ selectedCode }} 成交明细（{{ selectedTrades.length }} 笔）</h4>
            <div v-if="selectedTrades.length === 0" class="hint">该标的本区间无成交</div>
            <div v-else class="table-wrap">
              <table class="tbl">
                <thead>
                  <tr>
                    <th>日期</th>
                    <th>方向</th>
                    <th>数量</th>
                    <th>价格</th>
                    <th>手续费</th>
                    <th>盈亏</th>
                    <th>拒绝</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(t, i) in selectedTrades" :key="i">
                    <td class="mono-cell">{{ String(t.datetime).slice(0, 8) }}</td>
                    <td>{{ t.direction }}</td>
                    <td>{{ fmtInt(t.size) }}</td>
                    <td>{{ fmtNumber(t.price) }}</td>
                    <td>{{ fmtNumber(t.commission) }}</td>
                    <td :class="t.pnl >= 0 ? 'text-up' : 'text-down'">{{ fmtNumber(t.pnl) }}</td>
                    <td>{{ t.rejected ? '是' : '否' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </section>

        <section v-if="store.result.skipped.length" class="report-section panel">
          <h3>跳过标的（{{ store.result.skipped.length }}）</h3>
          <ul class="skip-list">
            <li v-for="s in store.result.skipped" :key="s.code">{{ s.code }}：{{ s.reason }}</li>
          </ul>
        </section>
      </div>
    </main>

    <!-- 单代码下钻弹窗（B-5：K线 + 买卖点 + 评级 + 19 项绩效） -->
    <div v-if="drillCode" class="modal-mask" @click.self="closeDrill">
      <div class="modal drill-modal">
        <div class="drawer-title">
          <h3>{{ drillCode }} 单代码下钻</h3>
          <button class="btn btn-ghost btn-xs" @click="closeDrill">关闭</button>
        </div>

        <section class="drill-section">
          <h4>K 线与买卖点</h4>
          <StateBlock v-if="klineLoading" state="loading" />
          <div v-else-if="klineError" class="drill-error">
            K 线加载失败：{{ klineError }}
            <p class="hint">（后端 /backtest/kline/{code} 未就绪时，本区暂不可用；评级与 19 项绩效仍基于 B4 结果展示）</p>
          </div>
          <div v-else-if="kline && kline.kline.length > 0">
            <KlineChart :kline="kline.kline" :buy-points="drillBuyPoints" :sell-points="drillSellPoints" height="420px" />
            <p class="hint">红▲ = 买入 · 绿▼ = 卖出（{{ kline.strategy }} 信号标注，非下单动作）</p>
          </div>
          <div v-else class="drill-error">
            K 线数据为空
            <p class="hint">（后端 /backtest/kline/{code} 未就绪或该标的本区间无行情；评级与 19 项绩效仍可用）</p>
          </div>
        </section>

        <section class="drill-section">
          <h4>绩效评级</h4>
          <GradeBadge v-if="drillGrade" :result="drillGrade" size="md" />
          <GradeDetails v-if="drillGrade" :result="drillGrade" compact />
        </section>

        <section class="drill-section">
          <h4>19 项绩效指标</h4>
          <div class="table-wrap">
            <table class="tbl perf19">
              <thead>
                <tr>
                  <th>分组</th>
                  <th>指标</th>
                  <th>数值</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="k in PERF_19_KEYS" :key="k.key">
                  <td class="perf-group">{{ k.group }}</td>
                  <td>{{ k.label }}</td>
                  <td class="num">{{ k.fmt(drillPerf?.[k.key] ?? null) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section v-if="drillCode" class="drill-section">
          <h4>成交明细（{{ (store.result?.per_code[drillCode]?.trades ?? []).length }} 笔）</h4>
          <div v-if="(store.result?.per_code[drillCode]?.trades ?? []).length === 0" class="hint">该标的本区间无成交</div>
          <div v-else class="table-wrap drill-trades">
            <table class="tbl">
              <thead>
                <tr>
                  <th>日期</th>
                  <th>方向</th>
                  <th>数量</th>
                  <th>价格</th>
                  <th>手续费</th>
                  <th>盈亏</th>
                  <th>拒绝</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(t, i) in store.result?.per_code[drillCode]?.trades ?? []" :key="i">
                  <td class="mono-cell">{{ String(t.datetime).slice(0, 8) }}</td>
                  <td>{{ t.direction }}</td>
                  <td>{{ fmtInt(t.size) }}</td>
                  <td>{{ fmtNumber(t.price) }}</td>
                  <td>{{ fmtNumber(t.commission) }}</td>
                  <td :class="t.pnl >= 0 ? 'text-up' : 'text-down'">{{ fmtNumber(t.pnl) }}</td>
                  <td>{{ t.rejected ? '是' : '否' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.backtest-page {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.config-panel {
  width: 340px;
  flex-shrink: 0;
  padding: 16px;
}
.config-section {
  margin-bottom: 18px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.config-section:last-of-type {
  border-bottom: none;
  margin-bottom: 12px;
}
.config-section h3 {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 10px;
}
.hint {
  color: var(--text-faint);
  font-size: 12px;
  margin: 6px 0 0;
}
.strategy-desc {
  color: var(--text-secondary);
  font-size: 12px;
  margin: 8px 0 0;
  line-height: 1.5;
}
.code-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.code-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--primary-weak);
  color: var(--primary);
  border-radius: 6px;
  padding: 2px 8px;
  font-family: var(--mono);
  font-size: 12px;
}
.chip-x {
  border: none;
  background: transparent;
  color: var(--primary);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0 2px;
}
.chip-x:hover {
  color: var(--red);
}
.param-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 10px;
}
.param-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.param-label {
  font-size: 12px;
  color: var(--text-secondary);
}
.row {
  display: flex;
  gap: 8px;
}
.field {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--text-secondary);
}
.run-btn {
  width: 100%;
  margin-top: 4px;
}
.report-panel {
  flex: 1;
  min-width: 0;
}
.error-banner {
  background: var(--red-weak);
  border: 1px solid var(--red);
  color: var(--red);
  padding: 10px 14px;
  border-radius: var(--radius);
  margin-bottom: 12px;
  font-size: 13px;
}
.placeholder {
  padding: 40px;
  text-align: center;
  color: var(--text-faint);
}
.result-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.result-title {
  font-size: 14px;
  font-weight: 600;
}
.report-section {
  padding: 14px 16px;
  margin-bottom: 12px;
}
.report-section h3 {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 12px;
}
.report-section h4 {
  font-size: 13px;
  font-weight: 600;
  margin: 12px 0 8px;
}
.trades-block {
  margin-top: 8px;
  border-top: 1px dashed var(--border);
  padding-top: 8px;
}
.mono-cell {
  font-family: var(--mono);
}
.text-up {
  color: var(--red);
}
.text-down {
  color: var(--green);
}
.skip-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--text-secondary);
}
.drill-modal {
  width: 860px;
  max-width: 96vw;
}
.drill-section {
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px dashed var(--border);
}
.drill-section:last-child {
  border-bottom: none;
  margin-bottom: 0;
}
.drill-section h4 {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 10px;
}
.drill-error {
  color: var(--red);
  font-size: 13px;
  padding: 8px 0;
}
.perf19 .perf-group {
  color: var(--text-secondary);
  font-size: 12px;
  width: 64px;
}
.drill-trades {
  max-height: 260px;
  overflow-y: auto;
}
</style>
