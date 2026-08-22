<script setup lang="ts">
/**
 * 组合回测结果展示面板（契约 v1.3 草案 P3 / S5 结果）。
 * 输入 PortfolioResult（组合回测=1 策略×N 标的；多策略组合回测=key "{label}@{symbol}"）。
 * 展示：组合指标卡 / 组合净值+回撤双轴曲线 / 资金分配（饼图）/ 各标的对比表（含迷你趋势）+ 导出 JSON。
 */
import { computed } from 'vue';
import type { EChartsCoreOption } from 'echarts/core';
import type { BacktestEquityPoint, PortfolioResult } from '../types';
import { fmtInt, fmtMoney, fmtNumber, fmtPct, fmtSignedPct } from '../utils/format';
import EChart from './EChart.vue';
import ExportButton from './ExportButton.vue';

const props = withDefaults(
  defineProps<{
    result: PortfolioResult;
    title?: string;
  }>(),
  { title: '组合回测结果' },
);

/* ---------- 组合指标卡（total_performance 优先；最大回撤缺失时从净值曲线 drawdown_pct 推导） ---------- */
const metricCards = computed(() => {
  const tp = props.result.total_performance ?? {};
  const curve = props.result.combined_equity ?? [];
  let maxDrawdown: number | null = (tp.max_drawdown as number | null | undefined) ?? null;
  if (maxDrawdown === null && curve.length > 0) {
    let m = 0;
    for (const p of curve) {
      const v = p.drawdown_pct ?? p.drawdown;
      if (typeof v === 'number' && v < m) m = v;
    }
    maxDrawdown = m;
  }
  return [
    { key: 'total_return', label: '总收益率', value: tp.total_return ?? null, fmt: fmtSignedPct },
    { key: 'annual_return', label: '年化收益', value: tp.annual_return ?? null, fmt: fmtSignedPct },
    { key: 'max_drawdown', label: '最大回撤', value: maxDrawdown, fmt: fmtPct },
    { key: 'sharpe', label: '夏普比率', value: (tp.sharpe as number | null | undefined) ?? null, fmt: fmtNumber },
    { key: 'total_stocks', label: '标的数', value: tp.total_stocks ?? null, fmt: fmtInt },
    { key: 'total_cash', label: '总资金', value: tp.total_cash ?? null, fmt: fmtMoney },
  ];
});

/* ---------- 组合净值 + 回撤（双轴） ---------- */
const combinedEquityOption = computed<EChartsCoreOption>(() => {
  const curve = props.result.combined_equity ?? [];
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['组合净值', '回撤'] },
    grid: { left: 70, right: 30, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: curve.map((p) => p.datetime) },
    yAxis: [
      { type: 'value', name: '净值', scale: true },
      { type: 'value', name: '回撤', scale: true },
    ],
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
    series: [
      {
        name: '组合净值',
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

/* ---------- 资金分配（饼图） ---------- */
const allocationOption = computed<EChartsCoreOption>(() => {
  const entries = Object.entries(props.result.equity_allocation ?? {});
  const total = entries.reduce((sum, [, v]) => sum + (typeof v === 'number' ? v : 0), 0);
  return {
    tooltip: {
      trigger: 'item',
      formatter: (p: { name: string; percent: number }) => `${p.name}: ${p.percent.toFixed(2)}%`,
    },
    legend: { type: 'scroll', bottom: 0, orient: 'horizontal' },
    series: [
      {
        name: '资金分配',
        type: 'pie',
        radius: ['38%', '62%'],
        center: ['50%', '45%'],
        data: entries.map(([k, v]) => ({
          name: k,
          value: total > 0 ? v : 0,
        })),
        label: { formatter: '{b}\n{d}%' },
      },
    ],
  };
});

/* ---------- 各标的对比表 ---------- */
const individualEntries = computed(() => Object.entries(props.result.individual_results ?? {}));

function allocationOf(key: string): number | null {
  const v = props.result.equity_allocation?.[key];
  return typeof v === 'number' ? v : null;
}

/** 迷你趋势：净值归一化到 1.0 的 SVG 折线点串 */
function sparklinePoints(curve: BacktestEquityPoint[], w = 120, h = 32): string {
  if (!curve.length) return '';
  const base = curve[0].total !== 0 ? curve[0].total : 1;
  const vals = curve.map((p) => p.total / base);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const step = w / (vals.length - 1 || 1);
  return vals
    .map((v, i) => `${(i * step).toFixed(1)},${(h - 3 - ((v - min) / range) * (h - 6)).toFixed(1)}`)
    .join(' ');
}

function sparkColor(perf: Record<string, number | null>): string {
  return (perf.total_return ?? 0) >= 0 ? '#16a34a' : '#dc2626';
}

/* ---------- 导出 ---------- */
async function exportResult(): Promise<{ blob: Blob; filename: string }> {
  return {
    blob: new Blob([JSON.stringify(props.result, null, 2)], { type: 'application/json; charset=utf-8' }),
    filename: `portfolio_result_${new Date().toISOString().slice(0, 10)}.json`,
  };
}
</script>

<template>
  <div class="portfolio-result">
    <div class="result-toolbar">
      <span class="result-title">{{ title }}</span>
      <ExportButton :fetcher="exportResult" label="导出JSON" />
    </div>

    <section class="report-section panel">
      <h3>组合绩效指标</h3>
      <div class="metric-cards">
        <div v-for="m in metricCards" :key="m.key" class="metric-card">
          <div class="metric-label">{{ m.label }}</div>
          <div class="metric-value">{{ m.fmt(m.value) }}</div>
        </div>
      </div>
    </section>

    <section class="report-section panel">
      <h3>组合净值曲线与回撤（金额求和）</h3>
      <EChart :option="combinedEquityOption" height="340px" />
    </section>

    <section class="report-section panel">
      <h3>资金分配（等权分仓）</h3>
      <div v-if="Object.keys(result.equity_allocation ?? {}).length" class="allocation-wrap">
        <EChart :option="allocationOption" height="280px" />
      </div>
      <div v-else class="hint">后端未返回资金分配明细</div>
    </section>

    <section class="report-section panel">
      <h3>各标的/策略对比（{{ individualEntries.length }}）</h3>
      <div v-if="individualEntries.length === 0" class="hint">后端未返回各标的明细</div>
      <div v-else class="table-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th>标的/策略</th>
              <th>资金占比</th>
              <th>总收益</th>
              <th>年化</th>
              <th>最大回撤</th>
              <th>夏普</th>
              <th>胜率</th>
              <th>成交</th>
              <th>趋势</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="[key, item] in individualEntries" :key="key">
              <td class="mono-cell">{{ key }}</td>
              <td>{{ allocationOf(key) !== null ? fmtPct(allocationOf(key)) : '-' }}</td>
              <td>{{ fmtSignedPct(item.performance.total_return ?? null) }}</td>
              <td>{{ fmtSignedPct(item.performance.annual_return ?? null) }}</td>
              <td>{{ fmtPct(item.performance.max_drawdown ?? null) }}</td>
              <td>{{ fmtNumber(item.performance.sharpe ?? null) }}</td>
              <td>{{ fmtPct(item.performance.win_rate ?? null) }}</td>
              <td>{{ fmtInt(item.performance.total_trades ?? null) }}</td>
              <td>
                <svg
                  v-if="item.equity_curve.length"
                  width="120"
                  height="32"
                  class="sparkline"
                  :aria-label="`${key} 净值趋势`"
                >
                  <polyline
                    :points="sparklinePoints(item.equity_curve)"
                    fill="none"
                    :stroke="sparkColor(item.performance)"
                    stroke-width="1.5"
                  />
                </svg>
                <span v-else class="hint">-</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
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
.mono-cell {
  font-family: var(--mono);
}
.hint {
  color: var(--text-faint);
  font-size: 12px;
}
.allocation-wrap {
  width: 100%;
}
.sparkline {
  display: block;
}
</style>
