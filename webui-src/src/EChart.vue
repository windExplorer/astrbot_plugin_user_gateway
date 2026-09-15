<script setup lang="ts">
// ECharts 轻封装：只注册用到的图表与组件（按需引入，避免整包打进单文件产物）。
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts/core";
import { BarChart, HeatmapChart, LineChart, PieChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  LineChart,
  BarChart,
  PieChart,
  HeatmapChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DataZoomComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

const props = defineProps<{
  option: Record<string, any>;
  height?: string;
  loading?: boolean;
}>();

const el = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;
let observer: ResizeObserver | null = null;

function render() {
  if (!el.value) return;
  if (!chart) chart = echarts.init(el.value, undefined, { renderer: "canvas" });
  chart.setOption(props.option || {}, true);
  if (props.loading) chart.showLoading("default", { text: "加载中", maskColor: "rgba(255,255,255,0.6)" });
  else chart.hideLoading();
}

onMounted(() => {
  render();
  if (el.value && typeof ResizeObserver !== "undefined") {
    observer = new ResizeObserver(() => chart?.resize());
    observer.observe(el.value);
  }
});

watch(() => props.option, render, { deep: true });
watch(() => props.loading, render);

onBeforeUnmount(() => {
  observer?.disconnect();
  observer = null;
  chart?.dispose();
  chart = null;
});

defineExpose({
  resize: () => chart?.resize(),
});
</script>

<template>
  <div ref="el" :style="{ width: '100%', height: props.height || '300px' }" />
</template>
