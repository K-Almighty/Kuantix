/**
 * 图表画线工具（useChartDrawing）：K线图与分时图共用的绘制逻辑。
 * - 趋势线/矩形/黄金分割：两次点击定位（第一锚点用临时圆点反馈）
 * - 水平线/文本：单次点击完成
 * - 数据坐标存储（gridIndex 0 的 [类目索引, Y值]），缩放自动跟随
 * - 绘制完成 emit 复位工具；数据集切换（resetKey 变化）清空画线
 */
import { computed, ref, watch, type Ref } from 'vue';
import type * as echarts from 'echarts/core';
import type { DrawTool } from '../types';

type ChartHandle = ReturnType<typeof echarts.init>;

interface TrendDrawing { kind: 'trend'; x1: number; y1: number; x2: number; y2: number }
interface HLineDrawing { kind: 'hline'; y: number }
interface RectDrawing { kind: 'rect'; x1: number; y1: number; x2: number; y2: number }
interface FibDrawing { kind: 'fib'; x1: number; y1: number; x2: number; y2: number }
interface TextDrawing { kind: 'text'; x: number; y: number; text: string }
type Drawing = TrendDrawing | HLineDrawing | RectDrawing | FibDrawing | TextDrawing;

const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

/** markLine/markArea/markPoint 三类标记的数据集（挂到 '_draw' series 上） */
export interface ChartDrawMarks {
  markLineData: Array<Record<string, unknown> | Array<Record<string, unknown>>>;
  markAreaData: Array<Array<Record<string, unknown>>>;
  markPointData: Array<Record<string, unknown>>;
}

export function useChartDrawing(options: {
  drawTool: () => DrawTool;
  /** 类目总数（点击索引钳制用） */
  barCount: () => number;
  /** 数据集标识：变化时清空画线 */
  resetKey: () => string;
  /** 工具复位（绘制完成后调用） */
  resetTool: () => void;
}): {
  onChartReady: (chart: ChartHandle) => void;
  clearDrawings: () => void;
  marks: Ref<ChartDrawMarks>;
} {
  const drawings = ref<Drawing[]>([]);
  const pending = ref<{ x: number; y: number } | null>(null);
  let chartHandle: ChartHandle | null = null;

  function onChartReady(chart: ChartHandle): void {
    chartHandle = chart;
    chart.getZr().on('click', (ev: unknown) =>
      onCanvasClick(ev as { offsetX: number; offsetY: number }),
    );
  }

  function onCanvasClick(ev: { offsetX: number; offsetY: number }): void {
    const chart = chartHandle;
    const tool = options.drawTool();
    if (!chart || tool === 'none' || options.barCount() === 0) return;
    let pt: [number, number];
    try {
      pt = chart.convertFromPixel({ gridIndex: 0 }, [ev.offsetX, ev.offsetY]) as [number, number];
    } catch {
      return;
    }
    if (!pt || !Number.isFinite(pt[0]) || !Number.isFinite(pt[1])) return;
    const n = options.barCount();
    const x = Math.max(0, Math.min(n - 1, Math.round(pt[0])));
    const y = Math.round(pt[1] * 1e4) / 1e4;
    if (tool === 'hline') {
      drawings.value.push({ kind: 'hline', y });
      options.resetTool();
    } else if (tool === 'text') {
      const text = window.prompt('输入标注文本：')?.trim();
      if (text) drawings.value.push({ kind: 'text', x, y, text });
      options.resetTool();
    } else if (tool === 'trend' || tool === 'rect' || tool === 'fib') {
      if (!pending.value) {
        pending.value = { x, y };
      } else {
        drawings.value.push({
          kind: tool,
          x1: pending.value.x,
          y1: pending.value.y,
          x2: x,
          y2: y,
        } as Drawing);
        pending.value = null;
        options.resetTool();
      }
    }
  }

  function clearDrawings(): void {
    drawings.value = [];
    pending.value = null;
  }

  // 数据集切换后旧画线坐标失效，清空；工具切换时丢弃未完成的第一个锚点
  watch(
    () => options.resetKey(),
    () => clearDrawings(),
  );
  watch(
    () => options.drawTool(),
    () => {
      pending.value = null;
    },
  );

  /** 渲染标记（含 pending 锚点反馈：第一点显示圆点 + 提示，避免"点了没反应"） */
  const marks = computed<ChartDrawMarks>(() => {
    const markLineData: Array<Record<string, unknown> | Array<Record<string, unknown>>> = [];
    const markAreaData: Array<Array<Record<string, unknown>>> = [];
    const markPointData: Array<Record<string, unknown>> = [];
    for (const d of drawings.value) {
      if (d.kind === 'trend') {
        markLineData.push([{ coord: [d.x1, d.y1], value: '' }, { coord: [d.x2, d.y2] }]);
      } else if (d.kind === 'hline') {
        markLineData.push({ yAxis: d.y, value: d.y.toFixed(2), lineStyle: { color: '#f59e0b' } });
      } else if (d.kind === 'rect') {
        markAreaData.push([{ coord: [d.x1, d.y1] }, { coord: [d.x2, d.y2] }]);
      } else if (d.kind === 'fib') {
        const xa = Math.min(d.x1, d.x2);
        const xb = Math.max(d.x1, d.x2);
        for (const l of FIB_LEVELS) {
          const yl = d.y1 + (d.y2 - d.y1) * l;
          markLineData.push([
            { coord: [xa, yl], value: `${(l * 100).toFixed(1)}% ${yl.toFixed(2)}` },
            { coord: [xb, yl] },
          ]);
        }
      } else if (d.kind === 'text') {
        markPointData.push({
          coord: [d.x, d.y],
          symbol: 'circle',
          symbolSize: 2,
          itemStyle: { color: '#f59e0b' },
          label: { show: true, formatter: d.text, position: 'top', color: '#f59e0b', fontSize: 11 },
        });
      }
    }
    if (pending.value) {
      markPointData.push({
        coord: [pending.value.x, pending.value.y],
        symbol: 'circle',
        symbolSize: 7,
        itemStyle: { color: '#f59e0b', borderColor: '#fff', borderWidth: 1 },
        label: {
          show: true,
          formatter: '已定第一点，点击第二点完成',
          position: 'top',
          color: '#b45309',
          fontSize: 10,
        },
      });
    }
    return { markLineData, markAreaData, markPointData };
  });

  return { onChartReady, clearDrawings, marks };
}

/** '_draw' series 模板：两个图表组件共用（yAxisIndex 由调用方指定） */
export function drawSeries(yAxisIndex: number, m: ChartDrawMarks): Record<string, unknown> {
  return {
    name: '_draw',
    type: 'line',
    data: [],
    xAxisIndex: 0,
    yAxisIndex,
    silent: true,
    z: 100,
    markLine: {
      silent: true,
      symbol: 'none',
      animation: false,
      lineStyle: { color: '#f59e0b', width: 1.2 },
      label: { show: true, position: 'end', fontSize: 9, color: '#b45309', formatter: '{c}' },
      data: m.markLineData,
    },
    markArea: {
      silent: true,
      animation: false,
      itemStyle: { color: 'rgba(59,130,246,0.10)' },
      data: m.markAreaData,
    },
    markPoint: { silent: true, animation: false, data: m.markPointData },
  };
}
