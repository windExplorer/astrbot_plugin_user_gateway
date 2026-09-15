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

import { apiLevels, apiPost, apiSetSubjectLevel, apiSubject, type LevelRow, type SubjectDetail } from "../api";
import EChart from "../EChart.vue";
import EffectSegment from "./EffectSegment.vue";
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
// 对象级「指令权限」（feature=command）：禁止 = 这个人/这个群用不了任何指令
const cmdEffect = ref<string>("inherit");

const cmdResolvedText = computed(() => {
  const m = detail.value?.command_master as any;
  if (!m) return "";
  const word = m.resolved === "deny" ? "禁止" : "放行";
  if (!m.layer || m.layer_label === "系统默认") {
    return `跟随系统默认（当前：${word}）`;
  }
  return `生效：${word}｜来源：${m.layer_label}`;
});

// 等级
const levels = ref<LevelRow[]>([]);
const levelId = ref(0);
const levelOptions = ref<{ label: string; value: number }[]>([{ label: "未分组", value: 0 }]);

// 额度快捷编辑
const qPeriod = ref<"day" | "month" | "total">("day");
const qLimit = ref<number>(0);
const qMode = ref<"enforce" | "observe">("enforce");

const isUser = computed(() => props.type === "user");

// 生效模型（等级路由）：说明「这个会话实际会走哪个提供商/模型」
const modelRouteText = computed(() => {
  const r = detail.value?.model_route as any;
  if (!r || !r.layer) return "未配置（跟随 AstrBot 默认模型）";
  if (!r.provider_id) return r.reason || "配置的模型当前不可用，本次走 AstrBot 默认模型";
  return `${r.provider_id}${r.used_fallback ? "（备用）" : ""}｜来源：${r.label}`;
});

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
    cmdEffect.value = String((d as any).command_master?.effect || "inherit");
    levelId.value = d.level_id || 0;
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

async function loadLevels() {
  try {
    const res = await apiLevels();
    const mine = (res.items || []).filter((l) => l.kind === props.type);
    levels.value = mine;
    levelOptions.value = [
      { label: "未分组", value: 0 },
      ...mine.map((l) => ({ label: `${l.name}（${l.members}）`, value: l.id })),
    ];
  } catch {
    /* 等级加载失败不影响其它面板 */
  }
}

async function saveLevel(v: number) {
  levelId.value = v;
  saving.value = true;
  try {
    await apiSetSubjectLevel([{ scope_type: props.type, scope_id: props.id, level_id: v || null }]);
    message.success(v ? "等级已更新" : "已取消等级");
    emit("changed");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

/** 档位链一行的展示文案。 */
function chainText(c: { limits: Record<string, any>; usage: Record<string, number>; effective: boolean }): string {
  const entries = Object.entries(c.limits || {});
  if (!entries.length) return "未配置";
  return entries
    .map(([period, row]) => {
      const label = period === "day" ? "每日" : period === "month" ? "每月" : "累计";
      const limit = Number(row?.limit_tokens || 0);
      const used = Number(c.usage?.[period] || 0);
      return `${label} ${fmtNum(used)}/${limit === 0 ? "不限" : fmtNum(limit)}${row?.mode === "observe" ? "（观察）" : ""}`;
    })
    .join("；");
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

async function saveCommandEffect(next: string) {
  cmdEffect.value = next;
  saving.value = true;
  try {
    await apiPost("/policy", {
      scope_type: props.type,
      scope_id: props.id,
      effect: next,
      feature: "command",
    });
    message.success(
      next === "deny"
        ? "已禁止该对象使用任何指令"
        : next === "allow"
          ? "已允许该对象使用指令"
          : "已恢复继承",
    );
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
    // 0 = 明确「不限」并占住档位（更粗的额度层不再参与），不是删除
    message.success(qLimit.value > 0 ? "额度已保存" : "已设为「不限」（占住档位）");
    emit("changed");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

async function deleteQuota() {
  saving.value = true;
  try {
    await apiPost("/quota", {
      scope_type: props.type,
      scope_id: props.id,
      period: qPeriod.value,
      limit_tokens: null,
    });
    message.success("已删除该周期的专属额度（恢复继承）");
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
    if (props.show) {
      loadLevels();
      load();
    }
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
            kind="user"
            :id="props.id"
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

          <n-card size="small" title="权限">
            <n-space vertical :size="10">
              <n-space align="center" :size="10">
                <span style="font-size: 13px; width: 56px">LLM</span>
                <effect-segment :effect="effect" :disabled="saving" @change="saveEffect" />
                <span style="font-size: 12px; opacity: 0.6">
                  三者互斥；「继承」= 不写专属规则，跟随等级 / 群 / 全局默认
                </span>
              </n-space>
              <n-space align="center" :size="10">
                <span style="font-size: 13px; width: 56px">指令</span>
                <effect-segment :effect="cmdEffect" :disabled="saving" @change="saveCommandEffect" />
                <span style="font-size: 12px; opacity: 0.6">
                  {{ cmdResolvedText }}（禁止 = 用不了任何指令）
                </span>
              </n-space>
              <n-space align="center" :size="10">
                <span style="font-size: 13px">所属等级</span>
                <n-select
                  :value="levelId"
                  size="small"
                  style="width: 200px"
                  :options="levelOptions"
                  :disabled="saving"
                  @update:value="saveLevel"
                />
                <span style="font-size: 12px; opacity: 0.6">等级可带默认权限与额度模板</span>
              </n-space>
              <span style="font-size: 12px; opacity: 0.65">
                生效模型：{{ modelRouteText }}
              </span>
              <span v-if="detail?.bot?.ts" style="font-size: 12px; opacity: 0.65">
                最后回复：{{ new Date(detail.bot.ts * 1000).toLocaleString() }}（{{ detail.bot.kind === "llm" ? "LLM 回复" : detail.bot.kind === "command" ? "指令回复" : "普通消息" }}）
              </span>
            </n-space>
          </n-card>

          <n-card size="small" title="token 额度">
            <n-space vertical :size="10">
              <div v-if="detail?.quota_chain?.length">
                <div style="font-size: 12px; opacity: 0.6; margin-bottom: 4px">
                  额度档位链（命中最具体的一层即止，更粗的层不再参与）：
                </div>
                <div
                  v-for="c in detail.quota_chain"
                  :key="c.layer"
                  :style="{ opacity: c.effective ? 1 : 0.55, lineHeight: 1.6 }"
                >
                  <n-space justify="space-between" :size="8">
                    <span style="font-size: 12.5px">
                      {{ c.label }}
                      <n-tag v-if="c.effective" size="tiny" type="info" :bordered="false">生效</n-tag>
                      <n-tag v-if="c.exceeded" size="tiny" type="error" :bordered="false">已超限</n-tag>
                    </span>
                    <span style="font-size: 12px; opacity: 0.75">{{ chainText(c) }}</span>
                  </n-space>
                </div>
              </div>

              <n-empty
                v-if="!detail?.quotas?.length"
                description="该对象没有专属额度（按等级 / 全局档位算，见上）"
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
                    {{ fmtNum(q.used_tokens) }} / {{ q.limit_tokens === 0 ? "不限" : fmtNum(q.limit_tokens) }}
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
                <n-button size="small" :loading="saving" @click="deleteQuota">删除该周期</n-button>
                <n-button size="small" :loading="saving" @click="resetQuota">清零用量</n-button>
              </n-space>
              <span style="font-size: 12px; opacity: 0.55">
                上限填 0 = 明确「不限」（占住档位）；要恢复继承请用「删除该周期」。口径见配置页「缓存 token 是否计入」。
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
