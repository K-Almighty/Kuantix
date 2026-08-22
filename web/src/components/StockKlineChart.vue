<script setup lang="ts">
/**
 * 个股 K 线图（StockKlineChart）：通达信风格，多指标叠加。
 * - 主图：蜡烛 + MA5/10/20/60（可独立开关）
 * - 成交量副图（涨红跌绿，可开关）
 * - MACD / KDJ / RSI 三个技术指标副图（可独立开关）
 * - tooltip 含 OHLC + 量额 + 换手率
 * 数据来自 StockDetail（后端 /stock/detail/{code}）。
 */
import { computed } from 'vue';
import type { EChartsCoreOption } from 'echarts/core';
import type { StockBar, StockIndicators } from '../types';
import EChart from './EChart.vue';

const props = withDefaults(
  defineProps<{
    bars: StockBar[];
    indicators?: StockIndicators;
    height?: string;
    showVolume?: boolean;
    showMa?: boolean;
    showMacd?: boolean;
    showKdj?: boolean;
    showRsi?: boolean;
    /** 上市日期（YYYY-MM-DD）；用于动态确定数据起点与展示锚点 */
    listingDate?: string;
  }>(),
  {
    indicators: () => ({}),
    height: '560px',
    showVolume: true,
    showMa: true,
    showMacd: true,
    showKdj: false,
    showRsi: false,
    listingDate: '',
  },
);

/**
 * 动态缩放起点（通达信风格：默认聚焦近期、可回溯全历史）。
 * - 数据量 ≤ MIN_FULL 根：全量展示（start=0）；
 * - 数据量 > MIN_FULL：默认展示最近 DEFAULT_SPAN 根（如日K≈1年），
 *   起点按实际占比换算，确保新股/老股都有合理锚点而非统一截断。
 */
const MIN_FULL = 300;
const DEFAULT_SPAN = 250;
const zoomStart = computed(() => {
  const n = props.bars.length;
  if (n <= MIN_FULL) return 0;
  return Math.max(0, ((n - DEFAULT_SPAN) / n) * 100);
});

const UP = '#ef4444'; // 涨（红，A股习惯）
const DOWN = '#22c55e'; // 跌（绿）

const dates = computed(() => props.bars.map((b) => b.datetime));
const candles = computed(() =>
  props.bars.map((b) => [b.open, b.close, b.low, b.high]),
);

function volColor(bar: StockBar): string {
  return bar.close >= bar.open ? UP : DOWN;
}

const volumes = computed(() =>
  props.bars.map((b, i) => ({
    value: b.vol,
    itemStyle: { color: volColor(b) },
    x: i,
  })),
);

/** 动态构建 grid（主图固定，副图随开关追加） */
const layout = computed(() => {
  const subPanels: string[] = [];
  if (props.showVolume) subPanels.push('volume');
  if (props.showMacd) subPanels.push('macd');
  if (props.showKdj) subPanels.push('kdj');
  if (props.showRsi) subPanels.push('rsi');

  const grids: Array<Record<string, unknown>> = [
    { left: 56, right: 16, top: 28, height: '46%' },
  ];
  const n = subPanels.length;
  if (n > 0) {
    const total = 100;
    const mainEnd = 46 + 2;
    const gap = 2;
    const each = (total - mainEnd - gap * (n - 1) - 4) / n;
    subPanels.forEach((_, idx) => {
      const top = mainEnd + (each + gap) * idx + 2;
      grids.push({ left: 56, right: 16, top: `${top}%`, height: `${each}%` });
    });
  }
  return { subPanels, grids };
});

const option = computed<EChartsCoreOption>(() => {
  const { subPanels, grids } = layout.value;
  const series: Array<Record<string, unknown>> = [];
  const xAxes: Array<Record<string, unknown>> = [];
  const yAxes: Array<Record<string, unknown>> = [];
  const legendData: string[] = [];

  // 主图 xAxis
  xAxes.push({
    type: 'category',
    data: dates.value,
    gridIndex: 0,
    boundaryGap: true,
    axisLabel: { show: false },
  });
  yAxes.push({
    type: 'value',
    scale: true,
    gridIndex: 0,
    splitLine: { show: true, lineStyle: { color: '#eee' } },
  });

  // 蜡烛
  const candleSeries: Record<string, unknown> = {
    name: 'K线',
    type: 'candlestick',
    data: candles.value,
    xAxisIndex: 0,
    yAxisIndex: 0,
    itemStyle: {
      color: UP,
      color0: DOWN,
      borderColor: UP,
      borderColor0: DOWN,
    },
  };
  // 上市日期标记线（动态起点锚点；仅当 listingDate 落在数据区间内时绘制）
  if (props.listingDate && dates.value.length) {
    const firstDate = dates.value[0].slice(0, 10);
    const lastDate = dates.value[dates.value.length - 1].slice(0, 10);
    if (props.listingDate >= firstDate && props.listingDate <= lastDate) {
      candleSeries.markLine = {
        symbol: 'none',
        silent: true,
        lineStyle: { color: '#9ca3af', type: 'dotted', width: 1 },
        label: {
          formatter: '上市',
          color: '#6b7280',
          fontSize: 10,
          position: 'insideStartTop',
        },
        data: [{ xAxis: props.listingDate }],
      };
    }
  }
  series.push(candleSeries);
  legendData.push('K线');

  // MA 线（主图叠加）
  const maMap: Array<[keyof StockIndicators, string, string]> = [
    ['ma5', 'MA5', '#f59e0b'],
    ['ma10', 'MA10', '#3b82f6'],
    ['ma20', 'MA20', '#a855f7'],
    ['ma60', 'MA60', '#14b8a6'],
  ];
  if (props.showMa) {
    for (const [key, name, color] of maMap) {
      const arr = props.indicators[key] as (number | null)[] | undefined;
      if (!arr) continue;
      series.push({
        name,
        type: 'line',
        data: arr,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1, color },
        connectNulls: false,
      });
      legendData.push(name);
    }
  }

  // 副图
  let gridIdx = 1;
  const axisIndexByPanel: Record<string, number> = {};

  for (const panel of subPanels) {
    const gi = gridIdx;
    axisIndexByPanel[panel] = gi;
    xAxes.push({
      type: 'category',
      data: dates.value,
      gridIndex: gi,
      boundaryGap: true,
      axisLabel: { show: panel === subPanels[subPanels.length - 1] },
    });
    yAxes.push({
      type: 'value',
      gridIndex: gi,
      splitLine: { show: false },
      scale: true,
    });
    gridIdx += 1;
  }

  // 成交量
  if (props.showVolume) {
    const gi = axisIndexByPanel['volume'];
    series.push({
      name: '成交量',
      type: 'bar',
      data: volumes.value,
      xAxisIndex: gi,
      yAxisIndex: gi,
      barWidth: '70%',
    });
    legendData.push('成交量');
  }

  // MACD
  if (props.showMacd && props.indicators.macd) {
    const gi = axisIndexByPanel['macd'];
    const { dif, dea, macd } = props.indicators.macd;
    series.push({
      name: 'DIF', type: 'line', data: dif, xAxisIndex: gi, yAxisIndex: gi,
      showSymbol: false, lineStyle: { width: 1, color: '#f59e0b' }, connectNulls: false,
    });
    series.push({
      name: 'DEA', type: 'line', data: dea, xAxisIndex: gi, yAxisIndex: gi,
      showSymbol: false, lineStyle: { width: 1, color: '#3b82f6' }, connectNulls: false,
    });
    series.push({
      name: 'MACD', type: 'bar', data: macd, xAxisIndex: gi, yAxisIndex: gi,
      itemStyle: {
        color: (p: { value: number | null }) =>
          p.value == null ? '#ccc' : (p.value as number) >= 0 ? UP : DOWN,
      },
    });
    legendData.push('DIF', 'DEA', 'MACD');
  }

  // KDJ
  if (props.showKdj && props.indicators.kdj) {
    const gi = axisIndexByPanel['kdj'];
    const { k, d, j } = props.indicators.kdj;
    for (const [nm, arr, col] of [
      ['K', k, '#f59e0b'], ['D', d, '#3b82f6'], ['J', j, '#a855f7'],
    ] as Array<[string, (number | null)[], string]>) {
      series.push({
        name: nm, type: 'line', data: arr, xAxisIndex: gi, yAxisIndex: gi,
        showSymbol: false, lineStyle: { width: 1, color: col }, connectNulls: false,
      });
    }
    legendData.push('K', 'D', 'J');
  }

  // RSI
  if (props.showRsi && props.indicators.rsi) {
    const gi = axisIndexByPanel['rsi'];
    const { rsi6, rsi12, rsi24 } = props.indicators.rsi;
    for (const [nm, arr, col] of [
      ['RSI6', rsi6, '#f59e0b'], ['RSI12', rsi12, '#3b82f6'], ['RSI24', rsi24, '#a855f7'],
    ] as Array<[string, (number | null)[], string]>) {
      series.push({
        name: nm, type: 'line', data: arr, xAxisIndex: gi, yAxisIndex: gi,
        showSymbol: false, lineStyle: { width: 1, color: col }, connectNulls: false,
      });
    }
    legendData.push('RSI6', 'RSI12', 'RSI24');
  }

  return {
    animation: false,
    legend: { data: legendData, top: 0, type: 'scroll' },
    // 全局十字光标联动（主图/副图同步 snap 到同一根 K）
    axisPointer: {
      link: subPanels.map((_, gi) => ({ xAxisIndex: gi })),
      label: { backgroundColor: '#666', precision: 2 },
      lineStyle: { color: '#888', width: 1, type: 'dashed' },
      snap: true,
      z: 100,
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross',
        crossStyle: { color: '#888', width: 1, type: 'dashed' },
        label: {
          backgroundColor: '#666',
          borderColor: '#666',
          color: '#fff',
          precision: 2,
        },
      },
      confine: true,
      backgroundColor: 'rgba(255,255,255,0.95)',
      borderColor: '#ddd',
      borderWidth: 1,
      textStyle: { color: '#222', fontSize: 12 },
      formatter: (params: unknown) =>
        formatTooltip(params as Array<Record<string, unknown>>),
    },
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    // 缩放/拖动：滚轮缩放 + 拖拽平移，约束最小/最大可视根数避免过度拉伸
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: xAxes.map((_, i) => i),
        start: zoomStart.value,
        end: 100,
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        moveOnMouseWheel: false,
        minValueSpan: 20,
        maxValueSpan: 1200,
        zoomLock: false,
        throttle: 50,
      },
      {
        type: 'slider',
        xAxisIndex: xAxes.map((_, i) => i),
        height: 18,
        bottom: 2,
        start: zoomStart.value,
        end: 100,
        minValueSpan: 20,
        maxValueSpan: 1200,
        brushSelect: false,
        handleSize: '120%',
        showDetail: false,
        dataBackground: { lineStyle: { color: '#bbb' }, areaStyle: { color: '#eef2ff' } },
        fillerColor: 'rgba(47,111,237,0.12)',
        selectedDataBackground: { lineStyle: { color: '#2f6fed' }, areaStyle: { color: 'rgba(47,111,237,0.1)' } },
      },
    ],
    series,
  };
});

function formatTooltip(params: Array<Record<string, unknown>>): string {
  const first = params[0] as { dataIndex: number } | undefined;
  if (!first) return '';
  const bar = props.bars[first.dataIndex];
  if (!bar) return '';
  const chg = bar.open ? ((bar.close - bar.open) / bar.open) * 100 : 0;
  const lines = [
    `<b>${bar.datetime}</b>`,
    `开 ${bar.open.toFixed(2)} · 高 ${bar.high.toFixed(2)}`,
    `低 ${bar.low.toFixed(2)} · 收 ${bar.close.toFixed(2)}`,
    `<span style="color:${chg >= 0 ? UP : DOWN}">涨跌 ${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>`,
    `量 ${bar.vol.toLocaleString('zh-CN')} · 额 ${bar.amount.toLocaleString('zh-CN')}`,
    `换手 ${(bar.turnover * 100).toFixed(2)}%`,
  ];
  return lines.join('<br/>');
}
</script>

<template>
  <EChart :option="option" :height="height" />
</template>
