<script setup lang="ts">
// 对象详情抽屉：好友 / 群的权限、额度与用量曲线。
// 权限改动即时生效（后端写库后重建内存规则缓存）；额度按周期逐条维护。
import { computed, ref, watch } from "vue";
import {
  NButton,
  NCard,
  NDataTable,
  NDrawer,
  NDrawerContent,
  NEmpty,
  NGrid,
  NGridItem,
  NInputNumber,
  NProgress,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpace,
  NSpin,
  NStatistic,
  NTag,
  useMessage,
} from "naive-ui";

import { apiPost, apiSubject, type SubjectDetail } from "../api";
import EChart from "../EChart.vue";
import EffectTag from "./EffectTag.vue";
import SubjectAvatar from "./SubjectAvatar.vue";

const props = defineProps<{
  show: boolean;
  type: "user" | "group";
  id: string;
}>();

const emit = defineEmits<{
  (e: "update:show", value: boolean): void;
  (e: "changed"): void;
}>();

const message = useMessage();
const loading = ref(false);
const saving = ref(false);
const detail = ref<SubjectDetail | null>(null);
const effect = ref<string>("inherit");

// 额度快捷编辑
const qPeriod = ref<"day" | "month" | "total">("day");
const qLimit = ref<number>(0);
const qMode = ref<"enforce" | "observe">("enforce");

const isUser = computed(() => props.type === "user");

const displayName = computed(() => {
  const info = detail.value?.info as any;
  if (!info) return props.id;
  if (isUser.value) return info.display_name || info.nickname || info.uin || props.id;
  return info.name || info.group_id || props.id;
});

const infoRows = computed(() => {
  const info = detail.value?.info as any;
  if (!info) return [] as { label: string; value: string }[];
  if (isUser.value) {
    return [
      { label: "QQ 号", value: String(info.uin || props.id) },
      { label: "昵称", value: String(info.nickname || "-") },
      { label: "备注", value: String(info.remark || "-") },
      { label: "同步时间", value: fmtTime(info.updated_at) },
    ];
  }
  return [
    { label: "群号", value: String(info.group_id || props.id) },
    { label: "群名", value: String(info.name || "-") },
    {
      label: "人数",
      value: info.max_member_count
        ? `${info.member_count} / ${info.max_member_count}`
        : String(info.member_count || 0),
    },
    { label: "群主", value: String(info.owner || "-") },
  ];
});

function fmtTime(ts?: number): string {
  return ts ? new Date(ts * 1000).toLocaleString() : "-";
}

function fmtNum(n: number | undefined | null): string {
  const v = Number(n || 0);
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(2) + "M";
  if (v >= 1_000) return (v / 1_000).toFixed(1) + "K";
  return String(v);
}

async function load() {
  if (!props.id) return;
  loading.value = true;
  try {
    const d = await apiSubject(props.type, props.id, 7);
    detail.value = d;
    effect.value = d.effect || "inherit";
    // 额度的初值：优先日额度，否则月、累计
    const rows = d.quotas || [];
    const pick = rows.find((q) => q.period === "day") || rows.find((q) => q.period === "month") || rows[0];
    if (pick) {
      qPeriod.value = pick.period;
      qLimit.value = pick.limit_tokens;
      qMode.value = pick.mode;
    } else {
      qPeriod.value = "day";
      qLimit.value = 0;
      qMode.value = "enforce";
    }
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

async function saveEffect(next: string) {
  effect.value = next;
  saving.value = true;
  try {
    await apiPost("/policy", {
      scope_type: props.type,
      scope_id: props.id,
      effect: next,
      feature: "llm",
    });
    message.success(`已设为「${next === "allow" ? "放行" : next === "deny" ? "禁止" : "继承"}」`);
    emit("changed");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

async function saveQuota() {
  saving.value = true;
  try {
    await apiPost("/quota", {
      scope_type: props.type,
      scope_id: props.id,
      period: qPeriod.value,
      limit_tokens: Number(qLimit.value || 0),
      mode: qMode.value,
    });
    message.success(qLimit.value > 0 ? "额度已保存" : "额度已删除");
    emit("changed");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

async function resetQuota() {
  saving.value = true;
  try {
    await apiPost("/quota/reset", { scope_type: props.type, scope_id: props.id });
    message.success("已清零该对象的全部周期用量");
    emit("changed");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

const chartOption = computed(() => {
  const rows = detail.value?.stats?.series || [];
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["token", "调用次数"], top: 0 },
    grid: { left: 54, right: 44, top: 34, bottom: 26 },
    xAxis: { type: "category", data: rows.map((r) => r.day) },
    yAxis: [
      { type: "value", name: "token" },
      { type: "value", name: "次数" },
    ],
    series: [
      {
        name: "token",
        type: "bar",
        barMaxWidth: 26,
        itemStyle: { borderRadius: [4, 4, 0, 0] },
        data: rows.map((r) => r.tokens),
      },
      { name: "调用次数", type: "line", smooth: true, yAxisIndex: 1, data: rows.map((r) => r.calls) },
    ],
  };
});

const quotaPercent = (used: number, limit: number) =>
  limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;

const recentColumns = [
  { title: "时间", key: "ts", width: 150, render: (row: any) => fmtTime(row.ts) },
  {
    title: "类型",
    key: "status",
    width: 90,
    render: (row: any) =>
      row.status === "denied"
        ? `拒绝(${row.deny_reason || "-"})`
        : row.deny_reason?.startsWith("observe:")
          ? "观察"
          : "正常",
  },
  { title: "模型", key: "model", width: 130 },
  { title: "token", key: "tok_total", width: 90 },
  { title: "延迟", key: "latency_ms", width: 90, render: (row: any) => `${row.latency_ms || 0}ms` },
];

// 近期流水补一个 token 合计列
const recentData = computed(() =>
  (detail.value?.recent || []).map((r: any) => ({
    ...r,
    tok_total: (r.tok_in_other || 0) + (r.tok_in_cached || 0) + (r.tok_out || 0),
  })),
);

watch(
  () => [props.show, props.id, props.type],
  () => {
    if (props.show) load();
  },
  { immediate: true },
);
</script>

<template>
  <n-drawer
    :show="props.show"
    :width="640"
    placement="right"
    @update:show="(v) => emit('update:show', v)"
  >
    <n-drawer-content closable>
      <template #header>
        <n-space align="center" :size="10">
          <subject-avatar
            v-if="isUser"
            :qq="props.id"
            :name="displayName"
            :size="30"
          />
          <span>{{ displayName }}</span>
          <effect-tag :effect="effect" />
        </n-space>
      </template>

      <n-spin :show="loading">
        <n-space vertical :size="14">
          <n-card size="small" :title="isUser ? '好友信息' : '群信息'">
            <n-grid :cols="2" :x-gap="12" :y-gap="6">
              <n-grid-item v-for="row in infoRows" :key="row.label">
                <div style="font-size: 12px; opacity: 0.6">{{ row.label }}</div>
                <div style="font-size: 13px">{{ row.value }}</div>
              </n-grid-item>
            </n-grid>
            <n-empty
              v-if="!detail?.info"
              description="本地暂无该对象的缓存（好友/群列表同步后可见）"
              style="padding: 12px 0"
            />
          </n-card>

          <n-card size="small" title="LLM 权限">
            <n-space align="center" :size="10">
              <n-radio-group :value="effect" :disabled="saving" @update:value="saveEffect">
                <n-radio-button value="allow">放行</n-radio-button>
                <n-radio-button value="deny">禁止</n-radio-button>
                <n-radio-button value="inherit">继承</n-radio-button>
              </n-radio-group>
              <span style="font-size: 12px; opacity: 0.6">
                「继承」= 走群级或全局默认策略
              </span>
            </n-space>
          </n-card>

          <n-card size="small" title="token 额度">
            <n-space vertical :size="10">
              <n-empty
                v-if="!detail?.quotas?.length"
                description="未配置额度（不限量）"
                style="padding: 8px 0"
              />
              <div v-for="q in detail?.quotas || []" :key="q.period">
                <n-space justify="space-between" align="center">
                  <span style="font-size: 13px">
                    {{ q.period === "day" ? "每日" : q.period === "month" ? "每月" : "累计" }}
                    <n-tag size="tiny" :type="q.mode === 'observe' ? 'warning' : 'success'" :bordered="false">
                      {{ q.mode === "observe" ? "观察" : "拦截" }}
                    </n-tag>
                  </span>
                  <span style="font-size: 12px; opacity: 0.75">
                    {{ fmtNum(q.used_tokens) }} / {{ fmtNum(q.limit_tokens) }}
                  </span>
                </n-space>
                <n-progress
                  type="line"
                  :percentage="quotaPercent(q.used_tokens, q.limit_tokens)"
                  :height="6"
                  :show-indicator="false"
                />
              </div>

              <n-space align="center" :size="8" style="margin-top: 4px">
                <n-select
                  v-model:value="qPeriod"
                  size="small"
                  style="width: 110px"
                  :options="[
                    { label: '每日', value: 'day' },
                    { label: '每月', value: 'month' },
                    { label: '累计', value: 'total' },
                  ]"
                />
                <n-input-number
                  v-model:value="qLimit"
                  size="small"
                  :min="0"
                  :step="10000"
                  style="width: 150px"
                  placeholder="token 上限"
                />
                <n-select
                  v-model:value="qMode"
                  size="small"
                  style="width: 100px"
                  :options="[
                    { label: '拦截', value: 'enforce' },
                    { label: '观察', value: 'observe' },
                  ]"
                />
                <n-button size="small" type="primary" :loading="saving" @click="saveQuota">保存</n-button>
                <n-button size="small" :loading="saving" @click="resetQuota">清零用量</n-button>
              </n-space>
              <span style="font-size: 12px; opacity: 0.55">
                上限填 0 表示删除该周期额度（不限量）。口径见配置页「缓存 token 是否计入」。
              </span>
            </n-space>
          </n-card>

          <n-card size="small" title="用量概览">
            <n-grid :cols="4" :x-gap="10" :y-gap="8">
              <n-grid-item>
                <n-statistic label="今日 token" :value="fmtNum(detail?.today?.tok_total)" />
              </n-grid-item>
              <n-grid-item>
                <n-statistic label="今日调用" :value="String(detail?.today?.calls || 0)" />
              </n-grid-item>
              <n-grid-item>
                <n-statistic label="7 日 token" :value="fmtNum(detail?.stats?.totals?.tok_total)" />
              </n-grid-item>
              <n-grid-item>
                <n-statistic label="7 日被拒" :value="String(detail?.stats?.totals?.denied || 0)" />
              </n-grid-item>
            </n-grid>
            <n-empty
              v-if="!detail?.stats?.series?.length"
              description="暂无用量数据（接入 LLM 后开始记录）"
              style="padding: 14px 0"
            />
            <e-chart v-else :option="chartOption" height="220px" />
          </n-card>

          <n-card size="small" title="近期流水">
            <n-empty v-if="!recentData.length" description="暂无记录" style="padding: 12px 0" />
            <n-data-table
              v-else
              :columns="recentColumns"
              :data="recentData"
              size="small"
              :bordered="false"
              :max-height="260"
            />
          </n-card>
        </n-space>
      </n-spin>
    </n-drawer-content>
  </n-drawer>
</template>
