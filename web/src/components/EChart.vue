<script setup lang="ts">
/** ECharts 封装：按容器尺寸自适应，option 变化自动重绘 */
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import * as echarts from 'echarts/core';
import { BarChart, LineChart, PieChart, ScatterChart, CandlestickChart, HeatmapChart } from 'echarts/charts';
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  TitleComponent,
  MarkLineComponent,
  MarkAreaComponent,
  MarkPointComponent,
  VisualMapComponent,
  GraphicComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsCoreOption } from 'echarts/core';

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  ScatterChart,
  CandlestickChart,
  HeatmapChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  TitleComponent,
  MarkLineComponent,
  MarkAreaComponent,
  MarkPointComponent,
  VisualMapComponent,
  GraphicComponent,
  CanvasRenderer,
]);

const emit = defineEmits<{
  /** 图表实例就绪（画线工具等需要直接访问 zr 事件/坐标换算的场景） */
  ready: [chart: ReturnType<typeof echarts.init>];
}>();

const props = withDefaults(
  defineProps<{
    option: EChartsCoreOption;
    height?: string;
    /** 数据集标识：变化时（如切换周期）重置缩放；不变时（如切换指标开关）保留用户当前缩放位置 */
    resetKey?: string;
  }>(),
  { height: '300px', resetKey: '' },
);

const el = ref<HTMLDivElement | null>(null);
const chart = shallowRef<ReturnType<typeof echarts.init> | null>(null);
let resizeObserver: ResizeObserver | null = null;
let lastResetKey = '';

function render(): void {
  const c = chart.value;
  if (!c) return;
  let opt = props.option;
  // 同一数据集上的重绘（如切换指标开关）：保留用户当前缩放位置，
  // 避免 notMerge 全量替换后视图跳回默认区间
  if (lastResetKey && lastResetKey === props.resetKey) {
    const prev = c.getOption() as
      | { dataZoom?: Array<{ start?: number; end?: number }> }
      | undefined;
    const prevZoom = Array.isArray(prev?.dataZoom) ? prev.dataZoom : [];
    const nextZoom = opt.dataZoom;
    if (prevZoom.length && Array.isArray(nextZoom)) {
      opt = {
        ...opt,
        dataZoom: (nextZoom as Array<Record<string, unknown>>).map(
          (dz: Record<string, unknown>, i: number) => {
            const p = prevZoom[i];
            return p && p.start != null && p.end != null
              ? { ...dz, start: p.start, end: p.end }
              : dz;
          },
        ),
      };
    }
  }
  lastResetKey = props.resetKey;
  c.setOption(opt, { notMerge: true });
}

onMounted(() => {
  if (!el.value) return;
  chart.value = echarts.init(el.value);
  render();
  emit('ready', chart.value);
  resizeObserver = new ResizeObserver(() => chart.value?.resize());
  resizeObserver.observe(el.value);
});

// option 每次都是 computed 生成的全新引用，浅比较即可；
// deep 会遍历数千个数据点，纯属浪费
watch(
  () => props.option,
  () => render(),
);

onBeforeUnmount(() => {
  resizeObserver?.disconnect();
  resizeObserver = null;
  chart.value?.dispose();
  chart.value = null;
});
</script>

<template>
  <div ref="el" class="echart" :style="{ height }"></div>
</template>
