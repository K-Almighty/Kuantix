<script setup lang="ts">
/**
 * 个股分时图（StockTimeShareChart）：通达信风格当日分时走势。
 * - 走势线（分钟收盘价）+ 淡面积填充 + 均价线（VWAP）
 * - 昨收基准虚线；Y 轴以昨收为中心对称（涨跌幅刻度右侧、价格刻度左侧）
 * - 成交量副图（红绿按分钟涨跌方向）
 * - 十字光标联动 + tooltip（时间/价格/涨跌幅/均价/量）
 * - 画线工具：趋势线/水平线/矩形/黄金分割/文本（与K线图共用 useChartDrawing）
 * 数据来自 /stock/detail/{code}?period=min1（bars + indicators.vwap）。
 */
import { computed } from 'vue';
import type { EChartsCoreOption } from 'echarts/core';
import type { DrawTool, StockBar } from '../types';
import { fmtBig } from '../utils/format';
import { drawSeries, useChartDrawing } from '../composables/useChartDrawing';
import EChart from './EChart.vue';

const props = withDefaults(
  defineProps<{
    bars: StockBar[];
    /** 分时均价线（当日累计 VWAP） */
    vwap?: (number | null)[];
    /** 昨收基准（Y 轴对称中心；缺失时退化为自适应范围） */
    prevClose?: number | null;
    height?: string;
    dark?: boolean;
    upColor?: string;
    downColor?: string;
    /** 画线工具（绘制完成后自动复位为 none） */
    drawTool?: DrawTool;
  }>(),
  {
    vwap: () => [],
    prevClose: null,
    height: '560px',
    dark: false,
    upColor: '#ef4444',
    downColor: '#22c55e',
    drawTool: 'none',
  },
);

const emit = defineEmits<{ 'update:drawTool': [tool: DrawTool] }>();

const up = computed(() => props.upColor);
const down = computed(() => props.downColor);

const theme = computed(() =>
  props.dark
    ? {
        split: '#272c38',
        label: '#8b95a5',
        tooltipBg: 'rgba(15,19,26,0.96)',
        tooltipBorder: '#2b3340',
        tooltipText: '#d7dfeb',
        areaFill: 'rgba(56,130,246,0.10)',
      }
    : {
        split: '#eef0f4',
        label: '#6b7280',
        tooltipBg: 'rgba(255,255,255,0.97)',
        tooltipBorder: '#d9dee6',
        tooltipText: '#1f2937',
        areaFill: 'rgba(47,111,237,0.08)',
      },
);

const times = computed(() => props.bars.map((b) => b.datetime.slice(11, 16)));
const closes = computed(() => props.bars.map((b) => b.close));

/** 分时 Y 轴范围：以昨收为中心对称（通达信语义）；无昨收时自适应 */
const yAxisRange = computed<{ min?: number; max?: number }>(() => {
  const pc = props.prevClose;
  if (pc == null || !pc || props.bars.length === 0) return {};
  let m = 0;
  for (const b of props.bars) {
    m = Math.max(m, Math.abs(b.high / pc - 1), Math.abs(b.low / pc - 1));
  }
  m = Math.max(m * 1.15, 0.005); // 上下留白，且避免横盘时范围塌缩
  return { min: pc * (1 - m), max: pc * (1 + m) };
});

/** 均价线数据对齐（后端 VWAP 与 bars 等长） */
const vwapData = computed(() => {
  if (props.vwap.length === props.bars.length) return props.vwap;
  return props.bars.map(() => null);
});

const volBars = computed(() =>
  props.bars.map((b, i) => {
    const ref = i > 0 ? props.bars[i - 1].close : (props.prevClose ?? b.open);
    return {
      value: b.vol,
      itemStyle: { color: b.close >= ref ? up.value : down.value },
    };
  }),
);

const resetKey = computed(
  () =>
    `${props.bars.length}:${props.bars[0]?.datetime ?? ''}:${
      props.bars[props.bars.length - 1]?.datetime ?? ''
    }`,
);

/* ---------------- 画线工具（与K线图共用逻辑） ---------------- */

const { onChartReady, clearDrawings, marks } = useChartDrawing({
  drawTool: () => props.drawTool,
  barCount: () => props.bars.length,
  resetKey: () => resetKey.value,
  resetTool: () => emit('update:drawTool', 'none'),
});

defineExpose({ clearDrawings });

function pctOf(v: number): string {
  const pc = props.prevClose;
  if (pc == null || !pc) return `${v.toFixed(2)}`;
  const p = (v / pc - 1) * 100;
  return `${p >= 0 ? '+' : ''}${p.toFixed(2)}%`;
}

const option = computed<EChartsCoreOption>(() => {
  const range = yAxisRange.value;
  const hasRange = range.min != null && range.max != null;

  return {
    animation: false,
    axisPointer: {
      link: [{ xAxisIndex: 0 }, { xAxisIndex: 1 }],
      label: { backgroundColor: '#5b6472' },
      lineStyle: { color: '#8a93a3', width: 1, type: 'dashed' },
      snap: true,
      z: 100,
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross',
        crossStyle: { color: '#8a93a3', width: 1, type: 'dashed' },
        label: { backgroundColor: '#5b6472', borderColor: '#5b6472', color: '#fff' },
      },
      confine: true,
      backgroundColor: theme.value.tooltipBg,
      borderColor: theme.value.tooltipBorder,
      borderWidth: 1,
      textStyle: { color: theme.value.tooltipText, fontSize: 12 },
      formatter: (params: unknown) => formatTooltip(params as Array<Record<string, unknown>>),
    },
    grid: [
      { left: 68, right: 64, top: 30, height: '56%' },
      { left: 68, right: 64, top: '72%', height: '18%' },
    ],
    xAxis: [
      {
        type: 'category',
        data: times.value,
        gridIndex: 0,
        boundaryGap: false,
        axisTick: { show: false },
        axisLine: { lineStyle: { color: theme.value.split } },
        axisLabel: { show: false },
      },
      {
        type: 'category',
        data: times.value,
        gridIndex: 1,
        boundaryGap: false,
        axisTick: { show: false },
        axisLine: { lineStyle: { color: theme.value.split } },
        axisLabel: { hideOverlap: true, color: theme.value.label, fontSize: 10 },
      },
    ],
    yAxis: [
      {
        type: 'value',
        gridIndex: 0,
        position: 'left',
        min: hasRange ? range.min : undefined,
        max: hasRange ? range.max : undefined,
        scale: !hasRange,
        splitNumber: 4,
        splitLine: { show: true, lineStyle: { color: theme.value.split } },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: theme.value.label, fontSize: 10 },
      },
      {
        type: 'value',
        gridIndex: 0,
        position: 'right',
        min: hasRange ? range.min : undefined,
        max: hasRange ? range.max : undefined,
        scale: !hasRange,
        splitNumber: 4,
        splitLine: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: theme.value.label, fontSize: 10, formatter: (v: number) => pctOf(v) },
      },
      {
        type: 'value',
        gridIndex: 1,
        splitLine: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: theme.value.label, fontSize: 10, formatter: (v: number) => fmtBig(v, 1) },
      },
    ],
    series: [
      {
        name: '价格',
        type: 'line',
        data: closes.value,
        xAxisIndex: 0,
        yAxisIndex: 0,
        showSymbol: false,
        lineStyle: { width: 1.2, color: '#2f6fed' },
        areaStyle: { color: theme.value.areaFill },
        markLine: {
          silent: true,
          symbol: 'none',
          animation: false,
          lineStyle: { color: '#9ca3af', type: 'dashed', width: 1 },
          label: {
            formatter: '昨收',
            color: theme.value.label,
            fontSize: 10,
            position: 'insideStartTop',
          },
          data: props.prevClose != null ? [{ yAxis: props.prevClose }] : [],
        },
      },
      {
        name: '均价',
        type: 'line',
        data: vwapData.value,
        xAxisIndex: 0,
        yAxisIndex: 0,
        showSymbol: false,
        lineStyle: { width: 1, color: '#eab308' },
        connectNulls: false,
      },
      {
        name: '成交量',
        type: 'bar',
        data: volBars.value,
        xAxisIndex: 1,
        yAxisIndex: 2,
        barWidth: '55%',
      },
      // 画线工具渲染（价格主图坐标系：xAxisIndex 0 / yAxisIndex 0）
      drawSeries(0, marks.value),
    ],
  };
});

function formatTooltip(params: Array<Record<string, unknown>>): string {
  const first = params[0] as { dataIndex: number } | undefined;
  if (!first) return '';
  const bar = props.bars[first.dataIndex];
  if (!bar) return '';
  const pc = props.prevClose;
  const chg = pc ? ((bar.close / pc - 1) * 100) : 0;
  const vw = vwapData.value[first.dataIndex];
  const lines = [
    `<b>${bar.datetime}</b>`,
    `价格 <span style="color:${chg >= 0 ? up.value : down.value}">${bar.close.toFixed(2)}</span>`,
  ];
  if (pc) {
    lines.push(
      `<span style="color:${chg >= 0 ? up.value : down.value}">涨跌 ${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>`,
    );
  }
  if (vw != null) lines.push(`均价 ${vw.toFixed(2)}`);
  lines.push(`量 ${fmtBig(bar.vol)}`);
  return lines.join('<br/>');
}
</script>

<template>
  <EChart :option="option" :height="height" :reset-key="resetKey" @ready="onChartReady" />
</template>
