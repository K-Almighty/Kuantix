<script setup lang="ts">
/**
 * K 线图（KlineChart）：蜡烛图 + 买卖点标注 + 成交量（契约 §3.8 B5 KlineWithSignals）。
 * - buy_points 红▲（箭头朝上，置于最低价下方）
 * - sell_points 绿▼（箭头朝下，置于最高价上方）
 * - SignalPoint.price 为 null 时取当日高低价占位（信号价 NaN→null 清洗后）
 * - 第二 grid 展示成交量（涨红跌绿）
 */
import { computed } from 'vue';
import type { EChartsCoreOption } from 'echarts/core';
import type { KlineBar, SignalPoint } from '../types';
import EChart from './EChart.vue';

const props = withDefaults(
  defineProps<{
    /** K 线数组（升序） */
    kline: KlineBar[];
    buyPoints?: SignalPoint[];
    sellPoints?: SignalPoint[];
    height?: string;
  }>(),
  { buyPoints: () => [], sellPoints: () => [], height: '420px' },
);

/** date → 索引映射（用于买卖点标注定位） */
const dateIndex = computed(() => {
  const m = new Map<string, number>();
  props.kline.forEach((b, i) => m.set(b.date, i));
  return m;
});

const option = computed<EChartsCoreOption>(() => {
  const dates = props.kline.map((b) => b.date);
  // ECharts candlestick 数据序：[open, close, lowest, highest]
  const candles = props.kline.map((b) => [b.open, b.close, b.low, b.high]);
  const volumes = props.kline.map((b, i) => ({
    value: b.vol,
    itemStyle: { color: b.close >= b.open ? '#ef4444' : '#22c55e' },
    x: i,
  }));

  const buyData = (props.buyPoints ?? [])
    .map((p) => {
      const idx = dateIndex.value.get(p.date);
      const bar = props.kline.find((b) => b.date === p.date);
      if (idx === undefined || !bar) return null;
      return [idx, p.price ?? bar.low * 0.985];
    })
    .filter((d): d is [number, number] => d !== null);

  const sellData = (props.sellPoints ?? [])
    .map((p) => {
      const idx = dateIndex.value.get(p.date);
      const bar = props.kline.find((b) => b.date === p.date);
      if (idx === undefined || !bar) return null;
      return [idx, p.price ?? bar.high * 1.015];
    })
    .filter((d): d is [number, number] => d !== null);

  return {
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: (params: unknown) => formatTooltip(params as Array<Record<string, unknown>>, props.kline),
    },
    legend: { data: ['K线', '买入', '卖出'], top: 0 },
    grid: [
      { left: 64, right: 24, top: 32, height: '58%' },
      { left: 64, right: 24, top: '74%', height: '16%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, boundaryGap: true },
      { type: 'category', data: dates, gridIndex: 1, boundaryGap: true, axisLabel: { show: false } },
    ],
    yAxis: [
      { type: 'value', scale: true, gridIndex: 0, splitLine: { show: true } },
      { type: 'value', gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], height: 18, bottom: 4, start: 0, end: 100 },
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: candles,
        itemStyle: {
          color: '#ef4444',
          color0: '#22c55e',
          borderColor: '#ef4444',
          borderColor0: '#22c55e',
        },
      },
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumes,
        barWidth: '70%',
      },
      {
        name: '买入',
        type: 'scatter',
        symbol: 'triangle',
        symbolSize: 11,
        symbolRotate: 0,
        data: buyData,
        itemStyle: { color: '#ef4444' },
        z: 10,
      },
      {
        name: '卖出',
        type: 'scatter',
        symbol: 'triangle',
        symbolSize: 11,
        symbolRotate: 180,
        data: sellData,
        itemStyle: { color: '#22c55e' },
        z: 10,
      },
    ],
  };
});

/** tooltip：显示 OHLC + 量额 + 当日买卖点 */
function formatTooltip(params: Array<Record<string, unknown>>, bars: KlineBar[]): string {
  const first = params[0];
  if (!first) return '';
  const dataIndex = Number(first.dataIndex);
  const bar = bars[dataIndex];
  if (!bar) return '';
  const lines = [
    `<b>${bar.date}</b>`,
    `开 ${bar.open.toFixed(2)} · 高 ${bar.high.toFixed(2)}`,
    `低 ${bar.low.toFixed(2)} · 收 ${bar.close.toFixed(2)}`,
    `量 ${bar.vol.toLocaleString('zh-CN')} · 额 ${bar.amount.toLocaleString('zh-CN')}`,
  ];
  const buy = (props.buyPoints ?? []).filter((p) => p.date === bar.date);
  const sell = (props.sellPoints ?? []).filter((p) => p.date === bar.date);
  if (buy.length) lines.push(`🔴 买入 ×${buy.length}`);
  if (sell.length) lines.push(`🟢 卖出 ×${sell.length}`);
  return lines.join('<br/>');
}
</script>

<template>
  <EChart :option="option" :height="height" />
</template>
