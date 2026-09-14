<script setup lang="ts">
// 限额：token 额度配置（对象 + 周期 + 上限 + 模式），支持新增 / 编辑 / 清零 / 删除。
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
  useMessage,
  type DataTableColumns,
} from "naive-ui";

import { apiGet, apiPost, type QuotaRow } from "../api";

const message = useMessage();
const loading = ref(false);
const rows = ref<QuotaRow[]>([]);
const showEditor = ref(false);
const saving = ref(false);

const form = ref({
  scope_type: "user" as "user" | "group",
  scope_id: "",
  period: "day" as "day" | "month" | "total",
  limit_tokens: 100000,
  mode: "enforce" as "enforce" | "observe",
});

const periodLabel: Record<string, string> = { day: "每日", month: "每月", total: "累计" };

async function load() {
  loading.value = true;
  try {
    const res = await apiGet<{ items: QuotaRow[] }>("/quota");
    rows.value = res.items || [];
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

function openEditor(row?: QuotaRow) {
  form.value = row
    ? {
        scope_type: row.scope_type as "user" | "group",
        scope_id: row.scope_id,
        period: row.period,
        limit_tokens: row.limit_tokens,
        mode: row.mode,
      }
    : { scope_type: "user", scope_id: "", period: "day", limit_tokens: 100000, mode: "enforce" };
  showEditor.value = true;
}

async function save() {
  if (!form.value.scope_id.trim()) {
    message.warning("请填写 QQ 号或群号");
    return;
  }
  saving.value = true;
  try {
    await apiPost("/quota", { ...form.value, scope_id: form.value.scope_id.trim() });
    message.success("已保存");
    showEditor.value = false;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

async function resetUsed(row: QuotaRow) {
  try {
    await apiPost("/quota/reset", {
      scope_type: row.scope_type,
      scope_id: row.scope_id,
      period: row.period,
    });
    message.success("已清零");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function remove(row: QuotaRow) {
  try {
    await apiPost("/quota", {
      scope_type: row.scope_type,
      scope_id: row.scope_id,
      period: row.period,
      limit_tokens: 0,
    });
    message.success("已删除该额度");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

function pct(row: QuotaRow): number {
  if (!row.limit_tokens) return 0;
  return Math.min(100, Math.round((Number(row.used_tokens || 0) / row.limit_tokens) * 100));
}

const columns: DataTableColumns<QuotaRow> = [
  {
    title: "对象",
    key: "scope_id",
    render: (row) =>
      h("div", { style: "line-height:1.3" }, [
        h("div", { style: "font-weight:500" }, row.scope_id),
        h("div", { style: "font-size:12px;opacity:.65" }, row.scope_type === "user" ? "QQ 好友" : "群聊"),
      ]),
  },
  { title: "周期", key: "period", width: 90, render: (row) => periodLabel[row.period] || row.period },
  {
    title: "模式",
    key: "mode",
    width: 100,
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
    width: 240,
    render: (row) =>
      h("div", { style: "min-width:190px" }, [
        h("div", { style: "font-size:12px;margin-bottom:2px" }, `${row.used_tokens} / ${row.limit_tokens}`),
        h(NProgress, { type: "line", percentage: pct(row), height: 6, showIndicator: false }),
      ]),
  },
  {
    title: "重置时间",
    key: "reset_at",
    width: 160,
    render: (row) =>
      row.reset_at ? new Date(row.reset_at * 1000).toLocaleString() : row.period === "total" ? "不重置" : "-",
  },
  {
    title: "操作",
    key: "actions",
    width: 170,
    render: (row) =>
      h(NSpace, { size: 6 }, {
        default: () => [
          h(NButton, { size: "tiny", onClick: () => openEditor(row) }, { default: () => "编辑" }),
          h(NButton, { size: "tiny", quaternary: true, onClick: () => resetUsed(row) }, { default: () => "清零" }),
          h(NButton, { size: "tiny", type: "error", ghost: true, onClick: () => remove(row) }, { default: () => "删除" }),
        ],
      }),
  },
];

const totalCount = computed(() => rows.value.length);

onMounted(load);
</script>

<template>
  <n-space vertical :size="14">
    <n-card size="small" title="额度说明">
      <n-space vertical :size="4" style="font-size: 13px; opacity: 0.8">
        <span>· 额度口径 = 输入 token + 缓存 token + 输出 token（「缓存计入」开关在配置页）。</span>
        <span>· 周期「每日 / 每月」按本地时间自动重置；「累计」不重置，需手动清零。</span>
        <span>· 「观察」模式只记账不拦截，适合上线前先看清用量。</span>
      </n-space>
    </n-card>

    <n-card size="small">
      <template #header>
        <n-space align="center" :size="10">
          <span>额度配置</span>
          <n-tag size="small" :bordered="false">{{ totalCount }} 条</n-tag>
        </n-space>
      </template>
      <template #header-extra>
        <n-space :size="8">
          <n-button size="small" @click="load">刷新</n-button>
          <n-button size="small" type="primary" @click="openEditor()">新增额度</n-button>
        </n-space>
      </template>

      <n-empty v-if="!loading && !rows.length" description="还没有配置任何额度" style="padding: 36px 0" />
      <n-data-table v-else :columns="columns" :data="rows" :loading="loading" :bordered="false" size="small" />
    </n-card>

    <n-modal v-model:show="showEditor" preset="card" title="额度配置" style="width: 460px">
      <n-form label-placement="left" label-width="92">
        <n-form-item label="对象类型">
          <n-radio-group v-model:value="form.scope_type">
            <n-radio-button value="user">QQ 好友</n-radio-button>
            <n-radio-button value="group">群聊</n-radio-button>
          </n-radio-group>
        </n-form-item>
        <n-form-item :label="form.scope_type === 'user' ? 'QQ 号' : '群号'">
          <n-input v-model:value="form.scope_id" placeholder="如 123456789" />
        </n-form-item>
        <n-form-item label="周期">
          <n-select
            v-model:value="form.period"
            :options="[
              { label: '每日', value: 'day' },
              { label: '每月', value: 'month' },
              { label: '累计', value: 'total' },
            ]"
          />
        </n-form-item>
        <n-form-item label="额度(token)">
          <n-input-number v-model:value="form.limit_tokens" :min="0" :step="10000" style="width: 100%" />
        </n-form-item>
        <n-form-item label="模式">
          <n-radio-group v-model:value="form.mode">
            <n-radio-button value="enforce">拦截</n-radio-button>
            <n-radio-button value="observe">观察</n-radio-button>
          </n-radio-group>
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showEditor = false">取消</n-button>
          <n-button type="primary" :loading="saving" @click="save">保存</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-space>
</template>
