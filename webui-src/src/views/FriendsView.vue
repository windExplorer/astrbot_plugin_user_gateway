<script setup lang="ts">
// 私聊：QQ 好友列表 + 逐个 / 批量权限管控。
// 好友数据来自协议端同步（M1 接入 /sync）；未同步时显示引导提示。
import { computed, h, onMounted, ref } from "vue";
import {
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NInput,
  NPagination,
  NProgress,
  NSpace,
  NTag,
  NTooltip,
  useMessage,
  type DataTableColumns,
} from "naive-ui";

import { apiGet, apiPost, type FriendRow, type Paged } from "../api";
import EffectTag from "../components/EffectTag.vue";
import SubjectAvatar from "../components/SubjectAvatar.vue";

const message = useMessage();
const loading = ref(false);
const rows = ref<FriendRow[]>([]);
const total = ref(0);
const page = ref(1);
const size = ref(50);
const keyword = ref("");
const effectFilter = ref("");
const checked = ref<string[]>([]);

async function load() {
  loading.value = true;
  try {
    const res = await apiGet<Paged<FriendRow>>(
      `/friends?page=${page.value}&size=${size.value}&q=${encodeURIComponent(keyword.value)}`,
    );
    rows.value = res.rows || [];
    total.value = res.total || 0;
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

const filtered = computed(() =>
  effectFilter.value ? rows.value.filter((r) => r.effect === effectFilter.value) : rows.value,
);

function search() {
  page.value = 1;
  load();
}

async function applyEffect(items: { scope_id: string; effect: string }[]) {
  if (!items.length) {
    message.warning("请先选择好友");
    return;
  }
  try {
    await apiPost("/policy", {
      items: items.map((i) => ({ scope_type: "user", scope_id: i.scope_id, effect: i.effect, feature: "llm" })),
    });
    message.success(`已更新 ${items.length} 个好友的权限`);
    checked.value = [];
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

function quotaText(row: FriendRow): string {
  if (row.quota_limit == null) return "未设额度";
  const used = Number(row.quota_used || 0);
  return `${used} / ${row.quota_limit}`;
}

function quotaPercent(row: FriendRow): number {
  if (!row.quota_limit) return 0;
  return Math.min(100, Math.round((Number(row.quota_used || 0) / row.quota_limit) * 100));
}

const columns: DataTableColumns<FriendRow> = [
  { type: "selection" },
  {
    title: "好友",
    key: "display_name",
    render: (row) =>
      h("div", { style: "display:flex;align-items:center;gap:10px" }, [
        h(SubjectAvatar, { qq: row.uin, name: row.display_name }),
        h("div", { style: "line-height:1.3" }, [
          h("div", { style: "font-weight:500" }, row.display_name || row.uin),
          h(
            "div",
            { style: "font-size:12px;opacity:.65" },
            `${row.uin}${row.remark ? " · 备注：" + row.remark : ""}`,
          ),
        ]),
      ]),
  },
  {
    title: "LLM 权限",
    key: "effect",
    width: 110,
    render: (row) => h(EffectTag, { effect: row.effect }),
  },
  {
    title: "今日额度",
    key: "quota",
    width: 220,
    render: (row) =>
      row.quota_limit == null
        ? h(NTag, { size: "small", bordered: false }, { default: () => "未设额度" })
        : h("div", { style: "min-width:170px" }, [
            h("div", { style: "font-size:12px;margin-bottom:2px" }, [
              quotaText(row),
              row.quota_mode === "observe" ? h("span", { style: "opacity:.6" }, " · 观察") : null,
            ]),
            h(NProgress, { type: "line", percentage: quotaPercent(row), height: 6, showIndicator: false }),
          ]),
  },
  {
    title: "操作",
    key: "actions",
    width: 210,
    render: (row) =>
      h(NSpace, { size: 6 }, {
        default: () => [
          h(NButton, { size: "tiny", type: row.effect === "allow" ? "primary" : "default", onClick: () => applyEffect([{ scope_id: row.uin, effect: "allow" }]) }, { default: () => "放行" }),
          h(NButton, { size: "tiny", type: row.effect === "deny" ? "error" : "default", onClick: () => applyEffect([{ scope_id: row.uin, effect: "deny" }]) }, { default: () => "禁止" }),
          h(NButton, { size: "tiny", quaternary: true, onClick: () => applyEffect([{ scope_id: row.uin, effect: "inherit" }]) }, { default: () => "继承" }),
        ],
      }),
  },
];

onMounted(load);
</script>

<template>
  <n-card size="small">
    <template #header>
      <n-space align="center" :size="10">
        <span>私聊好友</span>
        <n-tag size="small" :bordered="false">{{ total }} 个</n-tag>
      </n-space>
    </template>
    <template #header-extra>
      <n-space :size="8" align="center">
        <n-input
          v-model:value="keyword"
          size="small"
          placeholder="QQ 号 / 昵称 / 备注"
          style="width: 200px"
          clearable
          @keyup.enter="search"
        />
        <select v-model="effectFilter" class="plain-select">
          <option value="">全部权限</option>
          <option value="allow">放行</option>
          <option value="deny">禁止</option>
          <option value="inherit">继承</option>
        </select>
        <n-button size="small" @click="search">搜索</n-button>
        <n-button size="small" type="primary" :disabled="!checked.length" @click="applyEffect(checked.map((id) => ({ scope_id: id, effect: 'allow' })))">
          批量放行
        </n-button>
        <n-button size="small" type="error" ghost :disabled="!checked.length" @click="applyEffect(checked.map((id) => ({ scope_id: id, effect: 'deny' })))">
          批量禁止
        </n-button>
      </n-space>
    </template>

    <n-empty
      v-if="!loading && !total"
      description="还没有好友数据"
      style="padding: 40px 0"
    >
      <template #extra>
        <n-space vertical align="center" :size="6">
          <span style="font-size: 12px; opacity: 0.65">
            好友列表从协议端同步（需要在 AstrBot 里配置 aiocqhttp 适配器）。
          </span>
          <n-tag size="small" type="info" :bordered="false">同步能力随 M1 上线</n-tag>
        </n-space>
      </template>
    </n-empty>

    <template v-else>
      <n-data-table
        v-model:checked-row-keys="checked"
        :columns="columns"
        :data="filtered"
        :loading="loading"
        :row-key="(row: FriendRow) => row.uin"
        :bordered="false"
        size="small"
      />
      <n-space justify="end" style="margin-top: 12px">
        <n-pagination
          v-model:page="page"
          v-model:page-size="size"
          :item-count="total"
          :page-sizes="[20, 50, 100]"
          show-size-picker
          @update:page="load"
          @update:page-size="search"
        />
      </n-space>
    </template>
  </n-card>
</template>

<style scoped>
.plain-select {
  height: 28px;
  border-radius: 6px;
  border: 1px solid rgba(128, 128, 128, 0.35);
  background: transparent;
  padding: 0 6px;
  font-size: 13px;
}
</style>
