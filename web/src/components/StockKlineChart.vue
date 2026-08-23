<script setup lang="ts">
/**
 * 个股 K 线图（StockKlineChart）：通达信风格专业图表引擎。
 * - 主图：蜡烛 + 叠加指标单选（MA 窗口自定义 / EXPMA 前端计算 / BOLL / ENE / SAR）
 * - 副图：VOL / MACD / KDJ / RSI / WR / BIAS / OBV 独立开关，像素级多面板布局
 *   （每面板固定 110px，面板越多图表总高越大，避免压缩到看不清）
 * - 左右两侧均显示价格刻度（隐形高低线承接左轴范围，缩放联动）
 * - 十字光标全图联动 + 滚轮缩放 + 拖拽平移 + tooltip（OHLCV + MA）
 * - 画线工具：趋势线 / 水平线 / 矩形 / 黄金分割 / 文本标注（数据坐标，缩放自动跟随）
 * - 配色：红涨绿跌（A股习惯，固定）
 * 数据来自 StockDetail（后端 /stock/detail/{code}）。
 */
import { computed } from 'vue';
import type { EChartsCoreOption } from 'echarts/core';
import type { DrawTool, MainIndicator, StockBar, StockIndicators } from '../types';
import { fmtBig } from '../utils/format';
import { drawSeries, useChartDrawing } from '../composables/useChartDrawing';
import EChart from './EChart.vue';

type SubPanelKey = 'volume' | 'macd' | 'kdj' | 'rsi' | 'wr' | 'bias' | 'obv';

const props = withDefaults(
  defineProps<{
    bars: StockBar[];
    indicators?: StockIndicators;
    height?: string;
    /** 当前周期键：决定时间轴标签粒度 */
    period?: string;
    /** 主图叠加指标（单选；height 作为无副图时的基准总高，副图越多实际总高越大） */
    mainIndicator?: MainIndicator;
    /** MA 均线窗口（参数面板自定义） */
    maWindows?: number[];
    /** 副图指标开关 */
    showVolume?: boolean;
    showMacd?: boolean;
    showKdj?: boolean;
    showRsi?: boolean;
    showWr?: boolean;
    showBias?: boolean;
    showObv?: boolean;
    /** 深色主题 */
    dark?: boolean;
    /** 涨跌配色（默认红涨绿跌） */
    upColor?: string;
    downColor?: string;
    /** 画线工具（绘制完成后自动复位为 none） */
    drawTool?: DrawTool;
    /** 上市日期（YYYY-MM-DD）：动态起点锚点 */
    listingDate?: string;
  }>(),
  {
    indicators: () => ({}),
    height: '560px',
    period: 'day',
    mainIndicator: 'ma',
    maWindows: () => [5, 10, 20, 60, 365],
    showVolume: true,
    showMacd: true,
    showKdj: false,
    showRsi: false,
    showWr: false,
    showBias: false,
    showObv: false,
    dark: false,
    upColor: '#ef4444',
    downColor: '#22c55e',
    drawTool: 'none',
    listingDate: '',
  },
);

const emit = defineEmits<{ 'update:drawTool': [tool: DrawTool] }>();

const MA_COLORS = ['#f59e0b', '#3b82f6', '#a855f7', '#14b8a6', '#ef4444', '#0ea5e9', '#eab308', '#f472b6'];

const up = computed(() => props.upColor);
const down = computed(() => props.downColor);

/** 主题色板（深/浅） */
const theme = computed(() =>
  props.dark
    ? {
        split: '#272c38',
        label: '#8b95a5',
        tooltipBg: 'rgba(15,19,26,0.96)',
        tooltipBorder: '#2b3340',
        tooltipText: '#d7dfeb',
        zoomFiller: 'rgba(56,130,246,0.18)',
        zoomLine: '#3b4352',
        zoomArea: 'rgba(59,130,246,0.08)',
      }
    : {
        split: '#eef0f4',
        label: '#6b7280',
        tooltipBg: 'rgba(255,255,255,0.97)',
        tooltipBorder: '#d9dee6',
        tooltipText: '#1f2937',
        zoomFiller: 'rgba(47,111,237,0.12)',
        zoomLine: '#bbb',
        zoomArea: '#eef2ff',
      },
);

const dates = computed(() => props.bars.map((b) => b.datetime));
const candles = computed(() => props.bars.map((b) => [b.open, b.close, b.low, b.high]));

/**
 * 动态缩放起点（通达信风格：默认聚焦近期、可回溯全历史）。
 * - 数据量 ≤ MIN_FULL 根：全量展示；
 * - 分钟/分时数据：短窗口默认全量展示；
 * - 其余：默认展示最近 DEFAULT_SPAN 根（日K≈1年）。
 */
const MIN_FULL = 300;
const DEFAULT_SPAN = 250;
const zoomStart = computed(() => {
  const n = props.bars.length;
  if (n <= MIN_FULL) return 0;
  if (props.bars[n - 1]?.datetime.includes(' ')) return 0;
  return Math.max(0, ((n - DEFAULT_SPAN) / n) * 100);
});

/** 数据集标识：变化（切换周期/标的/复权）时重置缩放并清空画线 */
const resetKey = computed(
  () =>
    `${props.bars.length}:${props.bars[0]?.datetime ?? ''}:${
      props.bars[props.bars.length - 1]?.datetime ?? ''
    }`,
);

function volColor(bar: StockBar): string {
  return bar.close >= bar.open ? up.value : down.value;
}

const volumes = computed(() =>
  props.bars.map((b) => ({
    value: b.vol,
    itemStyle: { color: volColor(b) },
  })),
);

/**
 * 时间轴短标签（按周期粒度）：
 * - 年K：YYYY；季K：YYYY-Qn；月K：YYYY-MM；
 * - 日/周K：MM-DD，跨年首根显示 YYYY-MM；
 * - 分钟周期：同日 HH:MM，跨日首根显示 MM-DD。
 */
const axisLabels = computed(() => {
  if (props.period === 'year') return dates.value.map((d) => d.slice(0, 4));
  if (props.period === 'quarter') {
    return dates.value.map((d) => `${d.slice(0, 4)}-Q${Math.floor((Number(d.slice(5, 7)) - 1) / 3) + 1}`);
  }
  if (props.period === 'month') return dates.value.map((d) => d.slice(0, 7));
  const out: string[] = [];
  let lastDay = '';
  let lastYear = '';
  for (const dt of dates.value) {
    const day = dt.slice(0, 10);
    const year = dt.slice(0, 4);
    if (dt.includes(' ')) {
      out.push(day !== lastDay ? dt.slice(5, 10) : dt.slice(11, 16));
    } else if (props.period === 'day' || props.period === 'week') {
      out.push(year !== lastYear ? dt.slice(0, 7) : dt.slice(5, 10));
    } else {
      out.push(dt.slice(5, 10));
    }
    lastDay = day;
    lastYear = year;
  }
  return out;
});

/**
 * 像素级多面板布局（副图看不清的根治方案）：
 * - 每个副图面板固定 SUB_H 像素，不随面板数量压缩；
 * - 面板越多总高越大（页面滚动查看），主图随面板数轻微收缩但 ≥ MAIN_MIN；
 * - height prop 语义 = 无副图时的基准总高。
 */
const TOP_PAD = 26; // 主图指标标签行
const BOTTOM_PAD = 44; // 时间轴标签 + 缩放滑条
const SUB_H = 110; // 每个副图面板高度
const SUB_GAP = 6; // 面板间隙
const MAIN_MIN = 300; // 主图最小高度

function parseHeightPx(h: string): number {
  const m = /^(\d+(?:\.\d+)?)px$/.exec(h.trim());
  return m ? Number(m[1]) : 560;
}

const layout = computed(() => {
  const keys: SubPanelKey[] = [];
  if (props.showVolume) keys.push('volume');
  if (props.showMacd) keys.push('macd');
  if (props.showKdj) keys.push('kdj');
  if (props.showRsi) keys.push('rsi');
  if (props.showWr) keys.push('wr');
  if (props.showBias) keys.push('bias');
  if (props.showObv) keys.push('obv');

  const n = keys.length;
  const baseH = parseHeightPx(props.height);
  const mainH = Math.max(MAIN_MIN, baseH - TOP_PAD - BOTTOM_PAD - n * 30);
  const totalHeight = TOP_PAD + mainH + n * (SUB_H + SUB_GAP) + BOTTOM_PAD;

  const grids: Array<Record<string, unknown>> = [
    { left: 68, right: 60, top: TOP_PAD, height: mainH },
  ];
  const panels: Array<{ key: SubPanelKey; top: number }> = [];
  let top = TOP_PAD + mainH + SUB_GAP;
  for (const key of keys) {
    grids.push({ left: 68, right: 60, top, height: SUB_H });
    panels.push({ key, top });
    top += SUB_H + SUB_GAP;
  }
  return { grids, panels, totalHeight };
});

/** EXPMA 指数均线（通达信默认窗口 12/50；后端无此指标，前端从收盘价计算） */
const EXPMA_WINDOWS = [12, 50];
const EXPMA_COLORS = ['#f472b6', '#22d3ee'];

function emaFromCloses(closes: number[], span: number): (number | null)[] {
  const k = 2 / (span + 1);
  let prev: number | null = null;
  return closes.map((c) => {
    prev = prev == null ? c : c * k + prev * (1 - k);
    return prev;
  });
}

const expmaLines = computed(() => {
  const closes = props.bars.map((b) => b.close);
  return EXPMA_WINDOWS.map((w, i) => ({
    w,
    color: EXPMA_COLORS[i],
    data: emaFromCloses(closes, w),
  }));
});

/* ---------------- 画线工具 ---------------- */

const { onChartReady, clearDrawings, marks } = useChartDrawing({
  drawTool: () => props.drawTool,
  barCount: () => props.bars.length,
  resetKey: () => resetKey.value,
  resetTool: () => emit('update:drawTool', 'none'),
});

defineExpose({ clearDrawings });

function lastVal(arr: (number | null)[] | undefined): number | null {
  return arr?.[arr.length - 1] ?? null;
}

function fmt2(v: number | null): string {
  return v == null ? '--' : v.toFixed(2);
}

/* ---------------- option 组装 ---------------- */

const option = computed<EChartsCoreOption>(() => {
  const { grids, panels } = layout.value;
  const series: Array<Record<string, unknown>> = [];
  const xAxes: Array<Record<string, unknown>> = [];
  const yAxes: Array<Record<string, unknown>> = [];
  const graphic: Array<Record<string, unknown>> = [];
  const dim = theme.value.label;

  // 主图 xAxis（0）
  xAxes.push({
    type: 'category',
    data: dates.value,
    gridIndex: 0,
    boundaryGap: true,
    axisLabel: { show: false },
    axisTick: { show: false },
    axisLine: { lineStyle: { color: theme.value.split } },
  });
  // 主图 yAxis：0 = 左侧价格刻度；1 = 右侧价格刻度（同一范围双轴）
  yAxes.push({
    type: 'value',
    gridIndex: 0,
    position: 'left',
    scale: true,
    splitLine: { show: false },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: dim, fontSize: 10, formatter: (v: number) => v.toFixed(2) },
    axisPointer: { label: { formatter: (p: { value: number }) => p.value.toFixed(2) } },
  });
  yAxes.push({
    type: 'value',
    gridIndex: 0,
    position: 'right',
    scale: true,
    splitLine: { show: true, lineStyle: { color: theme.value.split } },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: dim, fontSize: 10, formatter: (v: number) => v.toFixed(2) },
  });

  // 蜡烛（主图，价格轴）
  const candleSeries: Record<string, unknown> = {
    name: 'K线',
    type: 'candlestick',
    data: candles.value,
    xAxisIndex: 0,
    yAxisIndex: 1,
    itemStyle: {
      color: up.value,
      color0: down.value,
      borderColor: up.value,
      borderColor0: down.value,
    },
  };
  if (props.listingDate && dates.value.length) {
    const firstDate = dates.value[0].slice(0, 10);
    const lastDate = dates.value[dates.value.length - 1].slice(0, 10);
    if (props.listingDate >= firstDate && props.listingDate <= lastDate) {
      candleSeries.markLine = {
        symbol: 'none',
        silent: true,
        lineStyle: { color: '#9ca3af', type: 'dotted', width: 1 },
        label: { formatter: '上市', color: dim, fontSize: 10, position: 'insideStartTop' },
        data: [{ xAxis: props.listingDate }],
      };
    }
  }
  series.push(candleSeries);

  // 左侧价格轴的隐形高低线（承接与蜡烛一致的范围，缩放联动）
  series.push({
    name: '_axisHigh',
    type: 'line',
    data: props.bars.map((b) => b.high),
    xAxisIndex: 0,
    yAxisIndex: 0,
    showSymbol: false,
    silent: true,
    z: 0,
    lineStyle: { opacity: 0 },
    emphasis: { disabled: true },
  });
  series.push({
    name: '_axisLow',
    type: 'line',
    data: props.bars.map((b) => b.low),
    xAxisIndex: 0,
    yAxisIndex: 0,
    showSymbol: false,
    silent: true,
    z: 0,
    lineStyle: { opacity: 0 },
    emphasis: { disabled: true },
  });

  // 主图指标标签行（通达信风格：指标名 + 最新值；单选，同一时刻只有一组）
  const mainSegs: Array<[string, string]> = [];
  if (props.mainIndicator === 'ma') {
    props.maWindows.forEach((w, i) => {
      const v = lastVal(props.indicators[`ma${w}`] as (number | null)[] | undefined);
      mainSegs.push([`MA${w}:${fmt2(v)}`, MA_COLORS[i % MA_COLORS.length]]);
    });
  } else if (props.mainIndicator === 'expma') {
    for (const l of expmaLines.value) {
      mainSegs.push([`EMA${l.w}:${fmt2(lastVal(l.data))}`, l.color]);
    }
  } else if (props.mainIndicator === 'boll' && props.indicators.boll) {
    const b = props.indicators.boll;
    mainSegs.push([
      `BOLL(20,2) UP:${fmt2(lastVal(b.upper))} MID:${fmt2(lastVal(b.mid))} LOW:${fmt2(lastVal(b.lower))}`,
      '#a78bfa',
    ]);
  } else if (props.mainIndicator === 'ene' && props.indicators.ene) {
    const e = props.indicators.ene;
    mainSegs.push([
      `ENE UP:${fmt2(lastVal(e.upper))} ENE:${fmt2(lastVal(e.ene))} LOW:${fmt2(lastVal(e.lower))}`,
      '#f472b6',
    ]);
  } else if (props.mainIndicator === 'sar' && props.indicators.sar) {
    mainSegs.push([`SAR:${fmt2(lastVal(props.indicators.sar.sar))}`, '#60a5fa']);
  }
  let gx = 72;
  for (const [text, color] of mainSegs) {
    graphic.push({
      type: 'text',
      left: gx,
      top: 4,
      silent: true,
      z: 300,
      style: { text, fill: color, fontSize: 11, fontFamily: 'ui-monospace, monospace' },
    });
    gx += text.length * 7 + 16;
  }

  // MA 均线（主图叠加，窗口自定义）
  if (props.mainIndicator === 'ma') {
    props.maWindows.forEach((w, i) => {
      const arr = props.indicators[`ma${w}`] as (number | null)[] | undefined;
      if (!arr) return;
      series.push({
        name: `MA${w}`,
        type: 'line',
        data: arr,
        xAxisIndex: 0,
        yAxisIndex: 1,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1, color: MA_COLORS[i % MA_COLORS.length] },
        connectNulls: false,
      });
    });
  }

  // EXPMA 指数均线（主图叠加，前端计算）
  if (props.mainIndicator === 'expma') {
    for (const l of expmaLines.value) {
      series.push({
        name: `EMA${l.w}`,
        type: 'line',
        data: l.data,
        xAxisIndex: 0,
        yAxisIndex: 1,
        showSymbol: false,
        lineStyle: { width: 1, color: l.color },
        connectNulls: false,
      });
    }
  }

  // BOLL 布林带（主图）
  if (props.mainIndicator === 'boll' && props.indicators.boll) {
    const { upper, mid, lower } = props.indicators.boll;
    for (const [nm, arr, col] of [
      ['BOLL_UP', upper, '#a78bfa'],
      ['BOLL_MID', mid, '#eab308'],
      ['BOLL_LOW', lower, '#4ade80'],
    ] as Array<[string, (number | null)[], string]>) {
      series.push({
        name: nm,
        type: 'line',
        data: arr,
        xAxisIndex: 0,
        yAxisIndex: 1,
        showSymbol: false,
        lineStyle: { width: 1, color: col },
        connectNulls: false,
      });
    }
  }

  // ENE 轨道线（主图）
  if (props.mainIndicator === 'ene' && props.indicators.ene) {
    const { upper, ene, lower } = props.indicators.ene;
    for (const [nm, arr, col] of [
      ['ENE_UP', upper, '#f472b6'],
      ['ENE', ene, '#38bdf8'],
      ['ENE_LOW', lower, '#f472b6'],
    ] as Array<[string, (number | null)[], string]>) {
      series.push({
        name: nm,
        type: 'line',
        data: arr,
        xAxisIndex: 0,
        yAxisIndex: 1,
        showSymbol: false,
        lineStyle: { width: 1, color: col, type: nm === 'ENE' ? 'solid' : 'dashed' },
        connectNulls: false,
      });
    }
  }

  // SAR 抛物线转向（主图散点）
  if (props.mainIndicator === 'sar' && props.indicators.sar) {
    series.push({
      name: 'SAR',
      type: 'scatter',
      data: props.indicators.sar.sar,
      xAxisIndex: 0,
      yAxisIndex: 1,
      symbolSize: 2.5,
      itemStyle: { color: '#60a5fa' },
    });
  }

  // 副图坐标（xAxisIndex = grid 序号；yAxisIndex 顺延主图双轴之后）
  panels.forEach((panel, idx) => {
    const gi = idx + 1;
    const yi = idx + 2;
    const fixed = panel.key === 'rsi' || panel.key === 'wr';
    xAxes.push({
      type: 'category',
      data: dates.value,
      gridIndex: gi,
      boundaryGap: true,
      axisTick: { show: false },
      axisLine: { lineStyle: { color: theme.value.split } },
      axisLabel: {
        show: idx === panels.length - 1,
        hideOverlap: true,
        color: dim,
        fontSize: 10,
        formatter: (_v: string, i: number) => axisLabels.value[i] ?? '',
      },
    });
    yAxes.push({
      type: 'value',
      gridIndex: gi,
      scale: !fixed,
      min: fixed ? 0 : undefined,
      max: fixed ? 100 : undefined,
      splitLine: { show: false },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: dim, fontSize: 10, formatter: (v: number) => fmtBig(v, 1) },
    });
  });

  const yiOf = (idx: number): number => idx + 2;

  // 成交量副图
  if (props.showVolume) {
    const idx = panels.findIndex((p) => p.key === 'volume');
    series.push({
      name: 'VOL',
      type: 'bar',
      data: volumes.value,
      xAxisIndex: idx + 1,
      yAxisIndex: yiOf(idx),
      barWidth: '60%',
    });
  }

  // MACD 副图
  if (props.showMacd && props.indicators.macd) {
    const idx = panels.findIndex((p) => p.key === 'macd');
    const { dif, dea, macd } = props.indicators.macd;
    series.push({
      name: 'DIF', type: 'line', data: dif, xAxisIndex: idx + 1, yAxisIndex: yiOf(idx),
      showSymbol: false, lineStyle: { width: 1, color: '#f59e0b' }, connectNulls: false,
    });
    series.push({
      name: 'DEA', type: 'line', data: dea, xAxisIndex: idx + 1, yAxisIndex: yiOf(idx),
      showSymbol: false, lineStyle: { width: 1, color: '#3b82f6' }, connectNulls: false,
    });
    series.push({
      name: 'MACD', type: 'bar', data: macd, xAxisIndex: idx + 1, yAxisIndex: yiOf(idx),
      itemStyle: {
        color: (p: { value: number | null }) =>
          p.value == null ? '#ccc' : (p.value as number) >= 0 ? up.value : down.value,
      },
    });
  }

  // KDJ 副图
  if (props.showKdj && props.indicators.kdj) {
    const idx = panels.findIndex((p) => p.key === 'kdj');
    const { k, d, j } = props.indicators.kdj;
    for (const [nm, arr, col] of [
      ['K', k, '#f59e0b'], ['D', d, '#3b82f6'], ['J', j, '#a855f7'],
    ] as Array<[string, (number | null)[], string]>) {
      series.push({
        name: nm, type: 'line', data: arr, xAxisIndex: idx + 1, yAxisIndex: yiOf(idx),
        showSymbol: false, lineStyle: { width: 1, color: col }, connectNulls: false,
      });
    }
  }

  // RSI 副图
  if (props.showRsi && props.indicators.rsi) {
    const idx = panels.findIndex((p) => p.key === 'rsi');
    const { rsi6, rsi12, rsi24 } = props.indicators.rsi;
    for (const [nm, arr, col] of [
      ['RSI6', rsi6, '#f59e0b'], ['RSI12', rsi12, '#3b82f6'], ['RSI24', rsi24, '#a855f7'],
    ] as Array<[string, (number | null)[], string]>) {
      series.push({
        name: nm, type: 'line', data: arr, xAxisIndex: idx + 1, yAxisIndex: yiOf(idx),
        showSymbol: false, lineStyle: { width: 1, color: col }, connectNulls: false,
      });
    }
  }

  // WR 副图
  if (props.showWr && props.indicators.wr) {
    const idx = panels.findIndex((p) => p.key === 'wr');
    const { wr6, wr10 } = props.indicators.wr;
    for (const [nm, arr, col] of [
      ['WR10', wr10, '#f59e0b'], ['WR6', wr6, '#3b82f6'],
    ] as Array<[string, (number | null)[], string]>) {
      series.push({
        name: nm, type: 'line', data: arr, xAxisIndex: idx + 1, yAxisIndex: yiOf(idx),
        showSymbol: false, lineStyle: { width: 1, color: col }, connectNulls: false,
      });
    }
  }

  // BIAS 副图
  if (props.showBias && props.indicators.bias) {
    const idx = panels.findIndex((p) => p.key === 'bias');
    const { bias6, bias12, bias24 } = props.indicators.bias;
    for (const [nm, arr, col] of [
      ['BIAS6', bias6, '#f59e0b'], ['BIAS12', bias12, '#3b82f6'], ['BIAS24', bias24, '#a855f7'],
    ] as Array<[string, (number | null)[], string]>) {
      series.push({
        name: nm, type: 'line', data: arr, xAxisIndex: idx + 1, yAxisIndex: yiOf(idx),
        showSymbol: false, lineStyle: { width: 1, color: col }, connectNulls: false,
      });
    }
  }

  // OBV 副图
  if (props.showObv && props.indicators.obv) {
    const idx = panels.findIndex((p) => p.key === 'obv');
    series.push({
      name: 'OBV', type: 'line', data: props.indicators.obv.obv, xAxisIndex: idx + 1, yAxisIndex: yiOf(idx),
      showSymbol: false, lineStyle: { width: 1, color: '#38bdf8' }, connectNulls: false,
    });
  }

  // 副图指标标签（面板左上角，指标名 + 最新值）
  const lastBar = props.bars[props.bars.length - 1];
  for (const panel of panels) {
    const segs: Array<[string, string]> = [];
    if (panel.key === 'volume' && lastBar) {
      segs.push(['VOL', dim]);
      segs.push([fmtBig(lastBar.vol), lastBar.close >= lastBar.open ? up.value : down.value]);
    } else if (panel.key === 'macd' && props.indicators.macd) {
      const m = props.indicators.macd;
      const mv = lastVal(m.macd);
      segs.push(['MACD(12,26,9)', dim]);
      segs.push([`DIF:${fmt2(lastVal(m.dif))}`, '#f59e0b']);
      segs.push([`DEA:${fmt2(lastVal(m.dea))}`, '#3b82f6']);
      segs.push([`MACD:${fmt2(mv)}`, mv != null && mv >= 0 ? up.value : down.value]);
    } else if (panel.key === 'kdj' && props.indicators.kdj) {
      const k = props.indicators.kdj;
      segs.push(['KDJ(9,3,3)', dim]);
      segs.push([`K:${fmt2(lastVal(k.k))}`, '#f59e0b']);
      segs.push([`D:${fmt2(lastVal(k.d))}`, '#3b82f6']);
      segs.push([`J:${fmt2(lastVal(k.j))}`, '#a855f7']);
    } else if (panel.key === 'rsi' && props.indicators.rsi) {
      const r = props.indicators.rsi;
      segs.push(['RSI(6,12,24)', dim]);
      segs.push([`RSI6:${fmt2(lastVal(r.rsi6))}`, '#f59e0b']);
      segs.push([`RSI12:${fmt2(lastVal(r.rsi12))}`, '#3b82f6']);
      segs.push([`RSI24:${fmt2(lastVal(r.rsi24))}`, '#a855f7']);
    } else if (panel.key === 'wr' && props.indicators.wr) {
      const w = props.indicators.wr;
      segs.push(['WR(10,6)', dim]);
      segs.push([`WR10:${fmt2(lastVal(w.wr10))}`, '#f59e0b']);
      segs.push([`WR6:${fmt2(lastVal(w.wr6))}`, '#3b82f6']);
    } else if (panel.key === 'bias' && props.indicators.bias) {
      const b = props.indicators.bias;
      segs.push(['BIAS(6,12,24)', dim]);
      segs.push([`BIAS6:${fmt2(lastVal(b.bias6))}`, '#f59e0b']);
      segs.push([`BIAS12:${fmt2(lastVal(b.bias12))}`, '#3b82f6']);
      segs.push([`BIAS24:${fmt2(lastVal(b.bias24))}`, '#a855f7']);
    } else if (panel.key === 'obv' && props.indicators.obv) {
      segs.push(['OBV', dim]);
      segs.push([fmtBig(lastVal(props.indicators.obv.obv)), '#38bdf8']);
    }
    let px = 72;
    for (const [text, color] of segs) {
      graphic.push({
        type: 'text',
        left: px,
        top: panel.top + 2,
        silent: true,
        z: 300,
        style: { text, fill: color, fontSize: 10, fontFamily: 'ui-monospace, monospace' },
      });
      px += text.length * 6.4 + 12;
    }
  }

  // 画线工具渲染（数据坐标 → markLine/markArea/markPoint，缩放自动跟随）
  series.push(drawSeries(1, marks.value));

  return {
    animation: false,
    graphic,
    // 全局十字光标联动（主图/副图同步 snap 到同一根 K）
    axisPointer: {
      link: xAxes.map((_, i) => ({ xAxisIndex: i })),
      label: { backgroundColor: '#5b6472', precision: 2 },
      lineStyle: { color: '#8a93a3', width: 1, type: 'dashed' },
      snap: true,
      z: 100,
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross',
        crossStyle: { color: '#8a93a3', width: 1, type: 'dashed' },
        label: { backgroundColor: '#5b6472', borderColor: '#5b6472', color: '#fff', precision: 2 },
      },
      confine: true,
      backgroundColor: theme.value.tooltipBg,
      borderColor: theme.value.tooltipBorder,
      borderWidth: 1,
      textStyle: { color: theme.value.tooltipText, fontSize: 12 },
      formatter: (params: unknown) => formatTooltip(params as Array<Record<string, unknown>>),
    },
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    // 缩放/拖动：滚轮缩放 + 拖拽平移，约束最小/最大可视根数
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
        borderColor: theme.value.split,
        dataBackground: { lineStyle: { color: theme.value.zoomLine }, areaStyle: { color: theme.value.zoomArea } },
        fillerColor: theme.value.zoomFiller,
        selectedDataBackground: {
          lineStyle: { color: theme.value.zoomLine },
          areaStyle: { color: theme.value.zoomArea },
        },
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
    `<span style="color:${chg >= 0 ? up.value : down.value}">涨跌 ${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>`,
    `量 ${fmtBig(bar.vol)} · 额 ${fmtBig(bar.amount)}`,
    `换手 ${bar.turnover > 0 ? `${(bar.turnover * 100).toFixed(2)}%` : '--'}`,
  ];
  if (props.mainIndicator === 'ma') {
    const maParts = props.maWindows
      .map((w, i) => {
        const arr = props.indicators[`ma${w}`] as (number | null)[] | undefined;
        const v = arr?.[first.dataIndex];
        if (v == null) return null;
        return `<span style="color:${MA_COLORS[i % MA_COLORS.length]}">MA${w} ${v.toFixed(2)}</span>`;
      })
      .filter((s): s is string => s !== null);
    if (maParts.length) lines.push(maParts.join(' '));
  }
  return lines.join('<br/>');
}
</script>

<template>
  <EChart :option="option" :height="`${layout.totalHeight}px`" :reset-key="resetKey" @ready="onChartReady" />
</template>
