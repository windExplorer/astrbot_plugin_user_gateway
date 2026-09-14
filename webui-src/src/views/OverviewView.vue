<script setup lang="ts">
// 总览：区间指标卡 + 趋势 / 模型占比 / 拒绝原因 / 活跃对象榜单。
import { computed, onMounted, ref } from "vue";
import {
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NGrid,
  NGridItem,
  NRadioButton,
  NRadioGroup,
  NSpace,
  NSpin,
  NStatistic,
  NTag,
  useMessage,
} from "naive-ui";

import { apiGet, type SummaryData } from "../api";
import EChart from "../EChart.vue";

const message = useMessage();
const loading = ref(false);
const range = ref<"1d" | "7d" | "30d">("7d");
const data = ref<SummaryData | null>(null);

const totals = computed(() => data.value?.totals);

function fmt(n: number | undefined | null): string {
  const v = Number(n || 0);
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(2) + "M";
  if (v >= 1_000) return (v / 1_000).toFixed(1) + "K";
  return String(v);
}

async function load() {
  loading.value = true;
  try {
    data.value = await apiGet<SummaryData>(`/overview?range=${range.value}`);
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

function setRange(v: "1d" | "7d" | "30d") {
  range.value = v;
  load();
}

onMounted(load);

// ------ 图表 option ------
const trendOption = computed(() => {
  const rows = data.value?.trend || [];
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["token", "调用次数", "被拒"], top: 0 },
    grid: { left: 48, right: 48, top: 34, bottom: 30 },
    xAxis: { type: "category", data: rows.map((r) => r.day), boundaryGap: false },
    yAxis: [
      { type: "value", name: "token" },
      { type: "value", name: "次数" },
    ],
    series: [
      {
        name: "token",
        type: "line",
        smooth: true,
        areaStyle: { opacity: 0.12 },
        data: rows.map((r) => r.tokens),
      },
      { name: "调用次数", type: "line", smooth: true, yAxisIndex: 1, data: rows.map((r) => r.calls) },
      { name: "被拒", type: "line", smooth: true, yAxisIndex: 1, data: rows.map((r) => r.denied) },
    ],
  };
});

const modelOption = computed(() => {
  const rows = (data.value?.by_model || []).filter((r) => Number(r.tokens) > 0);
  return {
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    legend: { type: "scroll", bottom: 0 },
    series: [
      {
        type: "pie",
        radius: ["42%", "68%"],
        center: ["50%", "44%"],
        itemStyle: { borderRadius: 4, borderColor: "transparent", borderWidth: 2 },
        label: { show: false },
        data: rows.map((r) => ({ name: r.model, value: r.tokens })),
      },
    ],
  };
});

const denyOption = computed(() => {
  const rows = data.value?.deny_reasons || [];
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 90, right: 30, top: 20, bottom: 24 },
    xAxis: { type: "value" },
    yAxis: { type: "category", data: rows.map((r) => r.reason) },
    series: [
      {
        type: "bar",
        barMaxWidth: 22,
        itemStyle: { borderRadius: [0, 4, 4, 0] },
        data: rows.map((r) => r.cnt),
      },
    ],
  };
});

const scopeColumns = [
  { title: "类型", key: "scope_type", width: 90 },
  { title: "对象", key: "scope_id" },
  { title: "token", key: "tokens", width: 110 },
  { title: "事件数", key: "events", width: 90 },
];
</script>

<template>
  <n-space vertical :size="16">
    <n-spin :show="loading">
      <n-grid :cols="4" :x-gap="12" :y-gap="12" item-responsive responsive="screen">
        <n-grid-item span="4 m:1">
          <n-card size="small" embedded>
            <n-statistic label="LLM 调用次数" :value="fmt(totals?.calls)" />
          </n-card>
        </n-grid-item>
        <n-grid-item span="4 m:1">
          <n-card size="small" embedded>
            <n-statistic label="token 总量" :value="fmt(totals?.tok_total)" />
          </n-card>
        </n-grid-item>
        <n-grid-item span="4 m:1">
          <n-card size="small" embedded>
            <n-statistic label="被拒次数" :value="fmt(totals?.denied)" />
          </n-card>
        </n-grid-item>
        <n-grid-item span="4 m:1">
          <n-card size="small" embedded>
            <n-statistic label="平均延迟(ms)" :value="Math.round(Number(totals?.avg_latency || 0))" />
          </n-card>
        </n-grid-item>
        <n-grid-item span="4 m:1">
          <n-card size="small" embedded>
            <n-statistic label="输入 token" :value="fmt(totals?.tok_in_other)" />
          </n-card>
        </n-grid-item>
        <n-grid-item span="4 m:1">
          <n-card size="small" embedded>
            <n-statistic label="缓存 token" :value="fmt(totals?.tok_in_cached)" />
          </n-card>
        </n-grid-item>
        <n-grid-item span="4 m:1">
          <n-card size="small" embedded>
            <n-statistic label="输出 token" :value="fmt(totals?.tok_out)" />
          </n-card>
        </n-grid-item>
        <n-grid-item span="4 m:1">
          <n-card size="small" embedded>
            <n-statistic label="活跃用户 / 群" :value="`${totals?.users || 0} / ${totals?.groups || 0}`" />
          </n-card>
        </n-grid-item>
      </n-grid>

      <n-space justify="space-between" align="center" style="margin: 16px 0 10px">
        <n-radio-group :value="range" size="small" @update:value="setRange">
          <n-radio-button value="1d">今日</n-radio-button>
          <n-radio-button value="7d">近 7 日</n-radio-button>
          <n-radio-button value="30d">近 30 日</n-radio-button>
        </n-radio-group>
        <n-space align="center" :size="8">
          <n-tag v-if="totals?.estimated" size="small" type="warning" :bordered="false">
            含 {{ totals.estimated }} 条估算用量
          </n-tag>
          <n-button size="small" @click="load">刷新</n-button>
        </n-space>
      </n-space>

      <n-card size="small" title="用量趋势">
        <n-empty v-if="!data?.trend?.length" description="暂无数据（统计从 M1 的 LLM 钩子接入后开始记录）" />
        <EChart v-else :option="trendOption" height="300px" />
      </n-card>

      <n-grid :cols="2" :x-gap="12" style="margin-top: 12px" item-responsive responsive="screen">
        <n-grid-item span="2 m:1">
          <n-card size="small" title="模型占比">
            <n-empty v-if="!modelOption.series[0].data.length" description="暂无数据" />
            <EChart v-else :option="modelOption" height="270px" />
          </n-card>
        </n-grid-item>
        <n-grid-item span="2 m:1">
          <n-card size="small" title="拒绝原因分布">
            <n-empty v-if="!denyOption.series[0].data.length" description="暂无拒绝记录" />
            <EChart v-else :option="denyOption" height="270px" />
          </n-card>
        </n-grid-item>
      </n-grid>

      <n-card size="small" title="用量 Top 对象" style="margin-top: 12px">
        <n-data-table
          :columns="scopeColumns"
          :data="data?.top_scopes || []"
          :bordered="false"
          size="small"
          :max-height="320"
        />
      </n-card>
    </n-spin>
  </n-space>
</template>
