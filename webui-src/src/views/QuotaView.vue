<script setup lang="ts">
// 限额：三层结构 —— 全局默认额度 → 自定义等级额度（模板）→ 对象专属额度。
// 解析规则见下方「额度说明」卡片，与后端闸门（gate.py）的档位链严格一致。
import { computed, h, onMounted, ref } from "vue";
import {
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NModal,
  NProgress,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpace,
  NTag,
  NTooltip,
  useMessage,
  type DataTableColumns,
} from "naive-ui";

import {
  apiDeleteLevel,
  apiGet,
  apiLevels,
  apiPost,
  apiProviders,
  apiSetLevel,
  type LevelPayload,
  type LevelRow,
  type ProviderRow,
  type QuotaRow,
} from "../api";

const message = useMessage();
const loading = ref(false);
const saving = ref(false);

const levels = ref<LevelRow[]>([]);
const globalRows = ref<QuotaRow[]>([]);
const specificRows = ref<QuotaRow[]>([]);
// AstrBot 里已加载的对话模型提供商（等级里「走哪个模型」只能从这里选：
// 不存在的提供商 id 会让 AstrBot 直接放弃本次请求）
const providers = ref<ProviderRow[]>([]);
const circuitOpen = ref<string[]>([]);

const providerOptions = computed(() =>
  providers.value.map((p) => ({
    label: `${p.model || p.id}${p.id ? " · " + p.id : ""}${circuitOpen.value.includes(p.id) ? "（熔断中）" : ""}`,
    value: p.id,
  })),
);

function providerLabel(id: string): string {
  if (!id) return "";
  const p = providers.value.find((x) => x.id === id);
  return p ? p.model || p.id : id;
}

const PERIODS: { value: "day" | "month" | "total"; label: string }[] = [
  { value: "day", label: "每日" },
  { value: "month", label: "每月" },
  { value: "total", label: "累计" },
];

const periodLabel: Record<string, string> = { day: "每日", month: "每月", total: "累计" };

function fmtNum(n: number | null | undefined): string {
  const v = Number(n || 0);
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(2) + "M";
  if (v >= 1_000) return (v / 1_000).toFixed(1) + "K";
  return String(v);
}

function rowOf(rows: QuotaRow[], period: string): QuotaRow | undefined {
  return rows.find((r) => r.period === period);
}

async function load() {
  loading.value = true;
  try {
    const [lv, glob, all, prov] = await Promise.all([
      apiLevels(),
      apiGet<{ items: QuotaRow[] }>("/quota?scope_type=global"),
      apiGet<{ items: QuotaRow[] }>("/quota"),
      apiProviders().catch(() => ({ items: [], circuit_open: [], route_enabled: true })),
    ]);
    levels.value = lv.items || [];
    globalRows.value = glob.items || [];
    specificRows.value = (all.items || []).filter((r) => r.scope_type === "user" || r.scope_type === "group");
    providers.value = prov.items || [];
    circuitOpen.value = prov.circuit_open || [];
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

// ------------------------------------------------------------------ //
// 全局默认额度
// ------------------------------------------------------------------ //
// 表单值：null = 不配置（该周期没有额度）；0 = 明确不限
const gForm = ref<Record<string, { limit: number | null; mode: "enforce" | "observe" }>>({
  day: { limit: null, mode: "enforce" },
  month: { limit: null, mode: "enforce" },
  total: { limit: null, mode: "enforce" },
});

function fillGlobalForm() {
  for (const p of PERIODS) {
    const row = rowOf(globalRows.value, p.value);
    gForm.value[p.value] = row
      ? { limit: row.limit_tokens, mode: row.mode }
      : { limit: null, mode: "enforce" };
  }
}

async function saveGlobal() {
  const items: Record<string, unknown>[] = [];
  for (const p of PERIODS) {
    const cur = rowOf(globalRows.value, p.value);
    const want = gForm.value[p.value];
    if (want.limit == null) {
      if (cur) items.push({ scope_type: "global", scope_id: "*", period: p.value, limit_tokens: null });
      continue;
    }
    if (!cur || cur.limit_tokens !== want.limit || cur.mode !== want.mode) {
      items.push({ scope_type: "global", scope_id: "*", period: p.value, limit_tokens: want.limit, mode: want.mode });
    }
  }
  if (!items.length) {
    message.info("全局额度没有变化");
    return;
  }
  saving.value = true;
  try {
    await apiPost("/quota", { items });
    message.success("全局额度已保存");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

// ------------------------------------------------------------------ //
// 等级管理
// ------------------------------------------------------------------ //
const showLevelEditor = ref(false);
const levelSaving = ref(false);
const levelForm = ref<
  Required<Omit<LevelPayload, "quotas">> & {
    quotas: Record<string, { limit: number | null; mode: "enforce" | "observe" }>;
  }
>({
  id: 0,
  kind: "user",
  name: "",
  description: "",
  effect: "inherit",
  sort_order: 0,
  provider_id: "",
  model: "",
  fallback_provider_id: "",
  quotas: { day: { limit: null, mode: "enforce" }, month: { limit: null, mode: "enforce" }, total: { limit: null, mode: "enforce" } },
});

function openLevelEditor(row?: LevelRow) {
  if (row) {
    levelForm.value = {
      id: row.id,
      kind: row.kind,
      name: row.name,
      description: row.description || "",
      effect: row.effect || "inherit",
      sort_order: row.sort_order || 0,
      provider_id: row.provider_id || "",
      model: row.model || "",
      fallback_provider_id: row.fallback_provider_id || "",
      quotas: {
        day: row.quotas?.day ? { limit: row.quotas.day.limit_tokens, mode: row.quotas.day.mode as "enforce" | "observe" } : { limit: null, mode: "enforce" },
        month: row.quotas?.month ? { limit: row.quotas.month.limit_tokens, mode: row.quotas.month.mode as "enforce" | "observe" } : { limit: null, mode: "enforce" },
        total: row.quotas?.total ? { limit: row.quotas.total.limit_tokens, mode: row.quotas.total.mode as "enforce" | "observe" } : { limit: null, mode: "enforce" },
      },
    };
  } else {
    levelForm.value = {
      id: 0,
      kind: "user",
      name: "",
      description: "",
      effect: "inherit",
      sort_order: (levels.value.length + 1) * 10,
      provider_id: "",
      model: "",
      fallback_provider_id: "",
      quotas: { day: { limit: null, mode: "enforce" }, month: { limit: null, mode: "enforce" }, total: { limit: null, mode: "enforce" } },
    };
  }
  showLevelEditor.value = true;
}

async function saveLevel() {
  const f = levelForm.value;
  if (!f.name.trim()) {
    message.warning("请填写等级名称");
    return;
  }
  levelSaving.value = true;
  try {
    const quotas = PERIODS.map((p) => {
      const want = f.quotas[p.value];
      // 留空(null) → 后端删除该周期；0 → 明确「不限」（占住档位）
      return { period: p.value, limit_tokens: want.limit, mode: want.mode };
    });
    await apiSetLevel({
      id: f.id || undefined,
      kind: f.kind,
      name: f.name.trim(),
      description: f.description?.trim() || "",
      effect: f.effect,
      sort_order: f.sort_order,
      provider_id: f.provider_id || "",
      model: f.model?.trim() || "",
      fallback_provider_id: f.fallback_provider_id || "",
      quotas,
    });
    message.success("等级已保存");
    showLevelEditor.value = false;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    levelSaving.value = false;
  }
}

async function removeLevel(row: LevelRow) {
  try {
    await apiDeleteLevel(row.id);
    message.success(`已删除等级「${row.name}」，其中 ${row.members} 个对象回到未分组`);
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

function levelQuotaText(row: LevelRow): string {
  const parts: string[] = [];
  for (const p of PERIODS) {
    const q = row.quotas?.[p.value];
    if (!q) continue;
    parts.push(`${periodLabel[p.value]} ${q.limit_tokens === 0 ? "不限" : fmtNum(q.limit_tokens)}`);
  }
  return parts.length ? parts.join(" / ") : "未配置（继续用更粗的档位）";
}

const effectText: Record<string, string> = { inherit: "继承", allow: "放行", deny: "禁止" };

const levelColumns: DataTableColumns<LevelRow> = [
  {
    title: "等级",
    key: "name",
    minWidth: 150,
    render: (row) =>
      h("div", { style: "line-height:1.35" }, [
        h("div", { style: "font-weight:500" }, row.name),
        row.description ? h("div", { style: "font-size:12px;opacity:.65" }, row.description) : null,
      ]),
  },
  {
    title: "适用",
    key: "kind",
    width: 80,
    render: (row) => h(NTag, { size: "small", bordered: false }, { default: () => (row.kind === "group" ? "群聊" : "好友") }),
  },
  {
    title: "默认 LLM 权限",
    key: "effect",
    width: 120,
    render: (row) =>
      h(
        NTooltip,
        { trigger: "hover" },
        {
          trigger: () =>
            h(
              NTag,
              { size: "small", bordered: false, type: row.effect === "deny" ? "error" : row.effect === "allow" ? "success" : "default" },
              { default: () => effectText[row.effect] || row.effect },
            ),
          default: () =>
            row.effect === "inherit"
              ? "该等级不管权限，只看更具体的规则与全局默认"
              : `属于该等级的对象默认${effectText[row.effect]}（优先级低于好友/群专属规则）`,
        },
      ),
  },
  {
    title: "模型",
    key: "provider_id",
    minWidth: 190,
    render: (row) => {
      if (!row.provider_id && !row.fallback_provider_id) {
        return h(NTag, { size: "small", bordered: false }, { default: () => "跟随 AstrBot 默认" });
      }
      const main = row.provider_id ? providerLabel(row.provider_id) : "未配置";
      const fb = row.fallback_provider_id ? providerLabel(row.fallback_provider_id) : "";
      return h(
        NTooltip,
        { trigger: "hover" },
        {
          trigger: () =>
            h("div", { style: "line-height:1.35" }, [
              h("div", { style: "font-size:12.5px" }, `${main}${row.model ? " · " + row.model : ""}`),
              fb ? h("div", { style: "font-size:12px;opacity:.65" }, `备用：${fb}`) : null,
            ]),
          default: () => `主提供商：${row.provider_id || "（未配置）"}${row.model ? "\n模型名：" + row.model : ""}\n备用提供商：${row.fallback_provider_id || "（未配置）"}`,
        },
      );
    },
  },
  { title: "额度模板", key: "quotas", minWidth: 200, render: (row) => levelQuotaText(row) },
  { title: "成员数", key: "members", width: 90 },
  {
    title: "操作",
    key: "actions",
    width: 130,
    render: (row) =>
      h(NSpace, { size: 6 }, {
        default: () => [
          h(NButton, { size: "tiny", onClick: () => openLevelEditor(row) }, { default: () => "编辑" }),
          h(NButton, { size: "tiny", type: "error", ghost: true, onClick: () => removeLevel(row) }, { default: () => "删除" }),
        ],
      }),
  },
];

// ------------------------------------------------------------------ //
// 对象专属额度
// ------------------------------------------------------------------ //
const showQuotaEditor = ref(false);
const quotaSaving = ref(false);
const quotaForm = ref({
  scope_type: "user" as "user" | "group",
  scope_id: "",
  period: "day" as "day" | "month" | "total",
  limit_tokens: 100000,
  mode: "enforce" as "enforce" | "observe",
});

function openQuotaEditor(row?: QuotaRow) {
  quotaForm.value = row
    ? {
        scope_type: row.scope_type as "user" | "group",
        scope_id: row.scope_id,
        period: row.period,
        limit_tokens: row.limit_tokens,
        mode: row.mode,
      }
    : { scope_type: "user", scope_id: "", period: "day", limit_tokens: 100000, mode: "enforce" };
  showQuotaEditor.value = true;
}

async function saveQuota() {
  if (!quotaForm.value.scope_id.trim()) {
    message.warning("请填写 QQ 号或群号");
    return;
  }
  quotaSaving.value = true;
  try {
    await apiPost("/quota", { ...quotaForm.value, scope_id: quotaForm.value.scope_id.trim() });
    message.success("已保存");
    showQuotaEditor.value = false;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    quotaSaving.value = false;
  }
}

async function resetUsed(row: QuotaRow) {
  try {
    await apiPost("/quota/reset", {
      scope_type: row.scope_type,
      scope_id: row.scope_id,
      period: row.period,
    });
    message.success("已清零该周期的用量");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function removeQuota(row: QuotaRow) {
  try {
    await apiPost("/quota", { scope_type: row.scope_type, scope_id: row.scope_id, period: row.period, limit_tokens: null });
    message.success("已删除该条限额");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

function pct(row: QuotaRow): number {
  if (!row.limit_tokens) return 0;
  return Math.min(100, Math.round((Number(row.used_tokens || 0) / row.limit_tokens) * 100));
}

const quotaColumns: DataTableColumns<QuotaRow> = [
  {
    title: "对象",
    key: "scope_id",
    minWidth: 160,
    render: (row) =>
      h("div", { style: "line-height:1.3" }, [
        h("div", { style: "font-weight:500" }, row.scope_id),
        h("div", { style: "font-size:12px;opacity:.65" }, row.scope_type === "user" ? "QQ 好友" : "群聊"),
      ]),
  },
  { title: "周期", key: "period", width: 80, render: (row) => periodLabel[row.period] || row.period },
  {
    title: "模式",
    key: "mode",
    width: 90,
    render: (row) =>
      h(
        NTag,
        { size: "small", type: row.mode === "observe" ? "warning" : "success", bordered: false },
        { default: () => (row.mode === "observe" ? "观察" : "拦截") },
      ),
  },
  {
    title: "用量",
    key: "used_tokens",
    minWidth: 200,
    render: (row) =>
      h("div", { style: "min-width:170px" }, [
        h("div", { style: "font-size:12px;margin-bottom:2px" }, `${fmtNum(row.used_tokens)} / ${fmtNum(row.limit_tokens)}`),
        h(NProgress, { type: "line", percentage: pct(row), height: 6, showIndicator: false, status: (row.used_tokens || 0) >= row.limit_tokens ? "error" : undefined }),
      ]),
  },
  {
    title: "重置时间",
    key: "reset_at",
    width: 150,
    render: (row) =>
      row.reset_at ? new Date(row.reset_at * 1000).toLocaleString() : row.period === "total" ? "不重置" : "-",
  },
  {
    title: "操作",
    key: "actions",
    width: 160,
    render: (row) =>
      h(NSpace, { size: 6 }, {
        default: () => [
          h(NButton, { size: "tiny", onClick: () => openQuotaEditor(row) }, { default: () => "编辑" }),
          h(NButton, { size: "tiny", quaternary: true, onClick: () => resetUsed(row) }, { default: () => "清零" }),
          h(NButton, { size: "tiny", type: "error", ghost: true, onClick: () => removeQuota(row) }, { default: () => "删除" }),
        ],
      }),
  },
];

const levelCount = computed(() => levels.value.length);

onMounted(load);
</script>

<template>
  <n-space vertical :size="14">
    <n-card size="small" title="额度说明">
      <n-space vertical :size="4" style="font-size: 13px; opacity: 0.82">
        <span>· 额度口径 = 输入 token + 缓存 token + 输出 token（「缓存计入」开关在配置页）。</span>
        <span>
          · 解析优先级：<b>好友专属 → 好友等级 → 群专属 → 群等级 → 全局</b>，
          <b>命中最具体的一层即止</b>——所以「VIP 等级 50 万」不会被「全局 5 万」反手拦掉。
        </span>
        <span>· 命中那一层内部，日 / 月 / 累计同时生效，任何一个超限就拦（模式为「观察」时只记账不拦）。</span>
        <span>· 留空 = 该周期不配置（继续看更粗的档位）；<b>填 0 = 明确不限</b>（占住档位，更粗的不再参与）。</span>
        <span>· 用量按「每个对象自己」记账（与是否配置额度无关），等级额度是「每人一份」，不是整组合计。</span>
        <span>
          · <b>模型路由</b>：等级可指定「主模型 + 备用模型」。<b>私聊看好友等级、群聊看群等级</b>
          （一个群只用一个模型，避免同群上下文串味）；主提供商不可用或连续失败熔断时自动走备用。
        </span>
      </n-space>
    </n-card>

    <!-- 全局默认额度 -->
    <n-card size="small">
      <template #header>
        <n-space align="center" :size="10">
          <span>全局默认额度</span>
          <n-tag size="small" :bordered="false">没有专属额度、也没有等级的对象按这份算</n-tag>
        </n-space>
      </template>
      <n-space vertical :size="10">
        <n-space v-for="p in PERIODS" :key="p.value" align="center" :size="10">
          <span style="width: 56px; font-size: 13px">{{ p.label }}</span>
          <n-input-number
            v-model:value="gForm[p.value].limit"
            size="small"
            :step="10000"
            placeholder="留空 = 不配置"
            style="width: 190px"
          />
          <n-radio-group v-model:value="gForm[p.value].mode" size="small">
            <n-radio-button value="enforce">拦截</n-radio-button>
            <n-radio-button value="observe">观察</n-radio-button>
          </n-radio-group>
          <n-tag
            v-if="rowOf(globalRows, p.value)"
            size="small"
            :bordered="false"
            :type="rowOf(globalRows, p.value)!.limit_tokens === 0 ? 'default' : 'info'"
          >
            当前：{{ rowOf(globalRows, p.value)!.limit_tokens === 0 ? "不限" : fmtNum(rowOf(globalRows, p.value)!.limit_tokens) }}
          </n-tag>
          <n-tag v-else size="small" :bordered="false">未配置</n-tag>
        </n-space>
        <n-space>
          <n-button size="small" type="primary" :loading="saving" @click="saveGlobal">保存全局额度</n-button>
          <span style="font-size: 12px; opacity: 0.6; align-self: center">
            群聊按「群」的用量算，私聊按「人」的用量算。
          </span>
        </n-space>
      </n-space>
    </n-card>

    <!-- 等级管理 -->
    <n-card size="small">
      <template #header>
        <n-space align="center" :size="10">
          <span>等级管理</span>
          <n-tag size="small" :bordered="false">{{ levelCount }} 个等级</n-tag>
        </n-space>
      </template>
      <template #header-extra>
        <n-space :size="8">
          <n-button size="small" @click="load">刷新</n-button>
          <n-button size="small" @click="openLevelEditor()">新增好友等级</n-button>
          <n-button size="small" @click="openLevelEditor({ id: 0, kind: 'group', name: '', description: '', effect: 'inherit', sort_order: 0, members: 0, quotas: {} } as any)">
            新增群聊等级
          </n-button>
        </n-space>
      </template>
      <n-empty v-if="!loading && !levels.length" description="还没有等级 —— 可以先建「普通 / VIP」两档试试" style="padding: 30px 0" />
      <n-data-table v-else :columns="levelColumns" :data="levels" :loading="loading" :bordered="false" size="small" />
    </n-card>

    <!-- 对象专属额度 -->
    <n-card size="small">
      <template #header>
        <n-space align="center" :size="10">
          <span>对象专属额度</span>
          <n-tag size="small" :bordered="false">{{ specificRows.length }} 条</n-tag>
        </n-space>
      </template>
      <template #header-extra>
        <n-button size="small" type="primary" @click="openQuotaEditor()">新增专属额度</n-button>
      </template>
      <n-empty v-if="!loading && !specificRows.length" description="还没有对象专属额度" style="padding: 30px 0" />
      <n-data-table v-else :columns="quotaColumns" :data="specificRows" :loading="loading" :bordered="false" size="small" />
    </n-card>

    <!-- 等级编辑弹窗 -->
    <n-modal v-model:show="showLevelEditor" preset="card" :title="levelForm.id ? '编辑等级' : '新增等级'" style="width: 560px">
      <n-form label-placement="left" label-width="110">
        <n-form-item label="适用">
          <n-radio-group v-model:value="levelForm.kind" :disabled="!!levelForm.id">
            <n-radio-button value="user">好友（私聊）</n-radio-button>
            <n-radio-button value="group">群聊</n-radio-button>
          </n-radio-group>
        </n-form-item>
        <n-form-item label="等级名称">
          <n-input v-model:value="levelForm.name" placeholder="如：普通 / VIP / 黑名单" />
        </n-form-item>
        <n-form-item label="说明">
          <n-input v-model:value="levelForm.description" placeholder="可选，给自己看的备注" />
        </n-form-item>
        <n-form-item label="默认 LLM 权限">
          <n-radio-group v-model:value="levelForm.effect">
            <n-radio-button value="inherit">继承</n-radio-button>
            <n-radio-button value="allow">放行</n-radio-button>
            <n-radio-button value="deny">禁止</n-radio-button>
          </n-radio-group>
          <span style="font-size: 12px; opacity: 0.6; margin-left: 8px">
            「继承」= 该等级只管额度，权限交给更具体的规则与全局默认
          </span>
        </n-form-item>
        <n-form-item label="排序值">
          <n-input-number v-model:value="levelForm.sort_order" size="small" style="width: 140px" />
        </n-form-item>
        <n-form-item label="主模型">
          <n-space vertical :size="6" style="width: 100%">
            <n-space align="center" :size="8">
              <n-select
                v-model:value="levelForm.provider_id"
                size="small"
                style="width: 260px"
                clearable
                placeholder="选择提供商（留空 = 跟随 AstrBot 默认）"
                :options="providerOptions"
              />
              <n-input
                v-model:value="levelForm.model"
                size="small"
                style="width: 170px"
                placeholder="模型名（可选）"
              />
            </n-space>
            <n-space align="center" :size="8">
              <span style="font-size: 12.5px; width: 48px">备用</span>
              <n-select
                v-model:value="levelForm.fallback_provider_id"
                size="small"
                style="width: 260px"
                clearable
                placeholder="主提供商不可用时用它（可选）"
                :options="providerOptions"
              />
            </n-space>
            <span style="font-size: 12px; opacity: 0.6; line-height: 1.5">
              私聊按「好友等级」、群聊按「群等级」决定模型（一个群一个模型，避免同群上下文串味）。<br />
              主提供商未加载或连续失败（熔断）时自动落到备用；模型名只作用于主提供商（备用用它自己的默认模型）。
            </span>
          </n-space>
        </n-form-item>
        <n-form-item label="额度模板">
          <n-space vertical :size="8" style="width: 100%">
            <n-space v-for="p in PERIODS" :key="p.value" align="center" :size="8">
              <span style="width: 44px; font-size: 13px">{{ p.label }}</span>
              <n-input-number
                v-model:value="levelForm.quotas[p.value].limit"
                size="small"
                :step="10000"
                placeholder="留空 = 不配置"
                style="width: 170px"
              />
              <n-radio-group v-model:value="levelForm.quotas[p.value].mode" size="small">
                <n-radio-button value="enforce">拦截</n-radio-button>
                <n-radio-button value="observe">观察</n-radio-button>
              </n-radio-group>
            </n-space>
            <span style="font-size: 12px; opacity: 0.6">
              每个属于该等级的对象各自一份额度（不是整组合计）；填 0 表示明确不限。
            </span>
          </n-space>
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showLevelEditor = false">取消</n-button>
          <n-button type="primary" :loading="levelSaving" @click="saveLevel">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 专属额度编辑弹窗 -->
    <n-modal v-model:show="showQuotaEditor" preset="card" title="专属额度" style="width: 460px">
      <n-form label-placement="left" label-width="92">
        <n-form-item label="对象类型">
          <n-radio-group v-model:value="quotaForm.scope_type">
            <n-radio-button value="user">QQ 好友</n-radio-button>
            <n-radio-button value="group">群聊</n-radio-button>
          </n-radio-group>
        </n-form-item>
        <n-form-item :label="quotaForm.scope_type === 'user' ? 'QQ 号' : '群号'">
          <n-input v-model:value="quotaForm.scope_id" placeholder="如 123456789" />
        </n-form-item>
        <n-form-item label="周期">
          <n-select
            v-model:value="quotaForm.period"
            :options="PERIODS.map((p) => ({ label: p.label, value: p.value }))"
          />
        </n-form-item>
        <n-form-item label="额度(token)">
          <n-input-number v-model:value="quotaForm.limit_tokens" :min="0" :step="10000" style="width: 100%" />
        </n-form-item>
        <n-form-item label="模式">
          <n-radio-group v-model:value="quotaForm.mode">
            <n-radio-button value="enforce">拦截</n-radio-button>
            <n-radio-button value="observe">观察</n-radio-button>
          </n-radio-group>
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showQuotaEditor = false">取消</n-button>
          <n-button type="primary" :loading="quotaSaving" @click="saveQuota">保存</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-space>
</template>
