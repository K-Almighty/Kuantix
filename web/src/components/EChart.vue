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
  MarkPointComponent,
  VisualMapComponent,
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
  MarkPointComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

const props = withDefaults(
  defineProps<{
    option: EChartsCoreOption;
    height?: string;
  }>(),
  { height: '300px' },
);

const el = ref<HTMLDivElement | null>(null);
const chart = shallowRef<ReturnType<typeof echarts.init> | null>(null);
let resizeObserver: ResizeObserver | null = null;

function render(): void {
  chart.value?.setOption(props.option, { notMerge: true });
}

onMounted(() => {
  if (!el.value) return;
  chart.value = echarts.init(el.value);
  render();
  resizeObserver = new ResizeObserver(() => chart.value?.resize());
  resizeObserver.observe(el.value);
});

watch(
  () => props.option,
  () => render(),
  { deep: true },
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
