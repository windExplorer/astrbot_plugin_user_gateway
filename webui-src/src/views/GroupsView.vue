<script setup lang="ts">
// 群聊：QQ 群列表 + 等级 / LLM 权限 / 生效额度 / 最后回复，支持逐个与批量管控。
// 群成员级管控（展开成员列表）按 PRD 排期在 M3，此处预留入口。
import { computed, h, onMounted, ref } from "vue";
import {
  NButton,
  NCard,
  NDataTable,
  NDropdown,
  NEmpty,
  NInput,
  NPagination,
  NProgress,
  NSelect,
  NSpace,
  NTag,
  NTooltip,
  useMessage,
  type DataTableColumns,
} from "naive-ui";

import {
  apiGet,
  apiLevels,
  apiPost,
  apiRefreshAvatars,
  apiSetSubjectLevel,
  apiSync,
  type GroupRow,
  type LevelRow,
  type Paged,
} from "../api";
import { dropAvatars, loadAvatars } from "../avatarStore";
import EffectSegment from "../components/EffectSegment.vue";
import PermissionHelp from "../components/PermissionHelp.vue";
import SubjectAvatar from "../components/SubjectAvatar.vue";
import SubjectDrawer from "../components/SubjectDrawer.vue";

const message = useMessage();
const loading = ref(false);
const syncing = ref(false);
const refreshingAvatars = ref(false);
const rows = ref<GroupRow[]>([]);
const total = ref(0);
const page = ref(1);
const size = ref(50);
const keyword = ref("");
const effectFilter = ref("");
const cmdFilter = ref("");
const sort = ref("active");
const checked = ref<string[]>([]);
const levels = ref<LevelRow[]>([]);
const batchLevelId = ref<number | null>(null);

// 详情抽屉
const drawerShow = ref(false);
const drawerId = ref("");

const levelOptions = computed(() => [
  { label: "未分组", value: 0 },
  ...levels.value.map((l) => ({ label: `${l.name}（${l.members}）`, value: l.id })),
]);

const sortOptions = [
  { label: "最近同步", value: "active" },
  { label: "最近回复", value: "last" },
  { label: "今日用量", value: "usage" },
  { label: "人数", value: "size" },
  { label: "群名", value: "name" },
  { label: "等级", value: "level" },
];

const KIND_META: Record<string, { text: string; type: "success" | "info" | "default" }> = {
  llm: { text: "LLM 回复", type: "success" },
  command: { text: "指令回复", type: "info" },
  normal: { text: "普通消息", type: "default" },
};

function kindMeta(kind: string) {
  return KIND_META[kind] || { text: kind || "未知", type: "default" as const };
}

function relTime(ts: number): string {
  if (!ts) return "";
  const diff = Math.max(0, Date.now() / 1000 - ts);
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  return new Date(ts * 1000).toLocaleDateString();
}

async function loadLevels() {
  try {
    const res = await apiLevels("group");
    levels.value = res.items || [];
  } catch {
    /* 等级加载失败不阻塞列表 */
  }
}

async function load() {
  loading.value = true;
  try {
    const res = await apiGet<Paged<GroupRow>>(
      `/groups?page=${page.value}&size=${size.value}&sort=${sort.value}` +
        `&effect=${encodeURIComponent(effectFilter.value)}` +
        `&effect_command=${encodeURIComponent(cmdFilter.value)}` +
        `&q=${encodeURIComponent(keyword.value)}`,
    );
    rows.value = res.rows || [];
    total.value = res.total || 0;
    loadAvatars("group", rows.value.map((r) => r.avatar_id || r.group_id));
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

async function syncNow() {
  syncing.value = true;
  try {
    const res = await apiSync();
    message.success(`同步完成：好友 ${res.total_friends} 个 / 群 ${res.total_groups} 个`);
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e), { duration: 6000 });
  } finally {
    syncing.value = false;
  }
}

async function refreshAvatars() {
  const ids = rows.value.map((r) => r.avatar_id || r.group_id).filter(Boolean);
  if (!ids.length) return;
  refreshingAvatars.value = true;
  try {
    const res = await apiRefreshAvatars("group", ids);
    dropAvatars("group", ids);
    await load();
    message.success(`群头像已更新：成功 ${res.refreshed} / 失败 ${res.failed}`);
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    refreshingAvatars.value = false;
  }
}

function search() {
  page.value = 1;
  load();
}

/**
 * 写权限规则。``feature`` 决定改的是哪一类：
 * ``"llm"`` = LLM 对话权限，``"command"`` = 指令权限（对象级总开关）。
 */
async function applyPolicy(items: { scope_id: string; effect: string }[], feature = "llm", silent = false) {
  if (!items.length) {
    message.warning("请先选择群");
    return;
  }
  const label = feature === "command" ? "指令权限" : "LLM 权限";
  try {
    await apiPost("/policy", {
      items: items.map((i) => ({ scope_type: "group", scope_id: i.scope_id, effect: i.effect, feature })),
    });
    if (!silent) message.success(`已更新 ${items.length} 个群的${label}`);
    checked.value = [];
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

function applyEffect(items: { scope_id: string; effect: string }[], silent = false) {
  return applyPolicy(items, "llm", silent);
}

async function applyLevel(items: { scope_id: string; level_id: number | null }[]) {
  if (!items.length) {
    message.warning("请先选择群");
    return;
  }
  try {
    await apiSetSubjectLevel(items.map((i) => ({ scope_type: "group", scope_id: i.scope_id, level_id: i.level_id })));
    message.success(`已更新 ${items.length} 个群的等级`);
    checked.value = [];
    batchLevelId.value = null;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

// 批量下拉：LLM 权限与指令权限是两套独立规则，分开列，避免点错
const batchOptions = [
  {
    type: "group",
    label: "LLM 权限",
    key: "g-llm",
    children: [
      { label: "放行", key: "llm:allow" },
      { label: "禁止", key: "llm:deny" },
      { label: "恢复继承", key: "llm:inherit" },
    ],
  },
  {
    type: "group",
    label: "指令权限",
    key: "g-cmd",
    children: [
      { label: "放行", key: "cmd:allow" },
      { label: "禁止（不能用任何指令）", key: "cmd:deny" },
      { label: "恢复继承", key: "cmd:inherit" },
    ],
  },
];

async function onBatch(key: string) {
  const [kind, effect] = String(key).split(":");
  if (!effect) return;
  await applyPolicy(
    checked.value.map((id) => ({ scope_id: id, effect })),
    kind === "cmd" ? "command" : "llm",
  );
}

function quotaPercent(row: GroupRow): number {
  if (!row.quota?.limit) return 0;
  return Math.min(100, Math.round(((row.quota.used || 0) / row.quota.limit) * 100));
}

function openDetail(row: GroupRow) {
  drawerId.value = row.group_id;
  drawerShow.value = true;
}

const columns: DataTableColumns<GroupRow> = [
  { type: "selection" },
  {
    title: "群",
    key: "name",
    minWidth: 190,
    render: (row) =>
      h(
        "div",
        {
          style: "display:flex;align-items:center;gap:10px;cursor:pointer",
          title: "点击查看详情",
          onClick: () => openDetail(row),
        },
        [
          h(SubjectAvatar, { kind: "group", id: row.group_id, name: row.name }),
          h("div", { style: "line-height:1.3" }, [
            h("div", { style: "font-weight:500" }, row.name || row.group_id),
            h("div", { style: "font-size:12px;opacity:.65" }, `群号 ${row.group_id}`),
          ]),
        ],
      ),
  },
  {
    title: "等级",
    key: "level_id",
    width: 130,
    render: (row) =>
      h(NSelect, {
        size: "tiny",
        value: row.level_id || 0,
        options: levelOptions.value,
        consistentMenuWidth: false,
        onUpdateValue: (v: number) => applyLevel([{ scope_id: row.group_id, level_id: v || null }]),
      }),
  },
  {
    title: "LLM 权限",
    key: "effect",
    width: 195,
    render: (row) =>
      h(EffectSegment, { effect: row.effect, onChange: (v: string) => applyEffect([{ scope_id: row.group_id, effect: v }]) }),
  },
  {
    title: "指令权限",
    key: "effect_command",
    width: 195,
    render: (row) =>
      h(NTooltip, { trigger: "hover", placement: "top" }, {
        trigger: () =>
          h(EffectSegment, {
            effect: row.effect_command || "inherit",
            onChange: (v: string) => applyPolicy([{ scope_id: row.group_id, effect: v }], "command"),
          }),
        default: () => "指令权限：禁止 = 这个群用不了任何指令（不含等级与单条指令的细则）",
      }),
  },
  {
    title: "生效额度",
    key: "quota",
    width: 200,
    render: (row) => {
      const q = row.quota;
      if (!q || !q.layer) {
        return h(NTag, { size: "small", bordered: false }, { default: () => "不限量" });
      }
      return h("div", { style: "min-width:160px" }, [
        h("div", { style: "font-size:12px;margin-bottom:2px;display:flex;justify-content:space-between;gap:8px" }, [
          h("span", [
            `${(q.used || 0) / 1000 >= 1 ? ((q.used || 0) / 1000).toFixed(1) + "K" : q.used || 0}` +
              ` / ${(q.limit / 1000).toFixed(0)}K`,
          ]),
          h(NTooltip, { trigger: "hover" }, {
            trigger: () => h(NTag, { size: "tiny", bordered: false, type: q.mode === "observe" ? "warning" : "default" }, { default: () => q.layer_label }),
            default: () => `生效档位：${q.layer_label}｜周期：${q.period === "day" ? "每日" : q.period === "month" ? "每月" : "累计"}${q.mode === "observe" ? "｜观察模式（只记账不拦截）" : ""}`,
          }),
        ]),
        h(NProgress, {
          type: "line",
          percentage: quotaPercent(row),
          height: 6,
          showIndicator: false,
          status: q.exceeded ? "error" : undefined,
        }),
      ]);
    },
  },
  {
    title: "今日用量",
    key: "today_tokens",
    width: 95,
    render: (row) => (row.today_tokens ? `${(row.today_tokens / 1000).toFixed(1)}K` : "-"),
  },
  {
    title: "人数",
    key: "member_count",
    width: 90,
    render: (row) =>
      h("span", row.max_member_count ? `${row.member_count} / ${row.max_member_count}` : String(row.member_count || 0)),
  },
  {
    title: "最后回复",
    key: "last_bot_ts",
    width: 140,
    render: (row) => {
      if (!row.last_bot_ts) {
        return h("span", { style: "opacity:.4" }, "无记录");
      }
      const meta = kindMeta(row.last_bot_kind);
      const tip = [
        new Date(row.last_bot_ts * 1000).toLocaleString(),
        row.last_bot_command ? `指令：${row.last_bot_command}` : "",
        row.last_bot_preview || "（无文本预览）",
      ]
        .filter(Boolean)
        .join("\n");
      return h(NTooltip, { trigger: "hover" }, {
        trigger: () =>
          h("div", { style: "line-height:1.35" }, [
            h("div", { style: "font-size:12px" }, relTime(row.last_bot_ts)),
            h(NTag, { size: "tiny", bordered: false, type: meta.type }, { default: () => meta.text }),
          ]),
        default: () => h("span", { style: "white-space:pre-line" }, tip),
      });
    },
  },
  {
    title: "",
    key: "actions",
    width: 70,
    render: (row) =>
      h(NButton, { size: "tiny", quaternary: true, onClick: () => openDetail(row) }, { default: () => "详情" }),
  },
];

onMounted(async () => {
  await loadLevels();
  await load();
});
</script>

<template>
  <permission-help kind="group" />

  <n-card size="small" style="margin-top: 12px">
    <template #header>
      <n-space align="center" :size="10">
        <span>群聊</span>
        <n-tag size="small" :bordered="false">{{ total }} 个</n-tag>
      </n-space>
    </template>
    <template #header-extra>
      <n-space :size="8" align="center">
        <n-input
          v-model:value="keyword"
          size="small"
          placeholder="群号 / 群名"
          style="width: 170px"
          clearable
          @keyup.enter="search"
        />
        <select v-model="effectFilter" class="plain-select" @change="search">
          <option value="">全部 LLM 权限</option>
          <option value="allow">LLM 放行</option>
          <option value="deny">LLM 禁止</option>
          <option value="inherit">LLM 继承</option>
        </select>
        <select v-model="cmdFilter" class="plain-select" @change="search">
          <option value="">全部指令权限</option>
          <option value="allow">指令放行</option>
          <option value="deny">指令禁止</option>
          <option value="inherit">指令继承</option>
        </select>
        <n-select
          v-model:value="sort"
          size="small"
          style="width: 118px"
          :options="sortOptions"
          @update:value="search"
        />
        <n-button size="small" @click="search">搜索</n-button>
        <n-button size="small" :loading="syncing" @click="syncNow">同步列表</n-button>
        <n-button size="small" :loading="refreshingAvatars" @click="refreshAvatars">更新头像</n-button>
      </n-space>
    </template>

    <n-space v-if="checked.length" align="center" :size="8" style="margin-bottom: 10px">
      <span style="font-size: 12px; opacity: 0.7">已选 {{ checked.length }} 个</span>
      <n-dropdown trigger="click" :options="batchOptions" @select="onBatch">
        <n-button size="small" type="primary" ghost>批量权限</n-button>
      </n-dropdown>
      <n-select
        v-model:value="batchLevelId"
        size="small"
        style="width: 160px"
        placeholder="批量设置等级…"
        clearable
        :options="levelOptions"
      />
      <n-button size="small" :disabled="batchLevelId == null" @click="applyLevel(checked.map((id) => ({ scope_id: id, level_id: batchLevelId || null })))">
        应用等级
      </n-button>
    </n-space>

    <n-empty v-if="!loading && !total" description="还没有群数据" style="padding: 40px 0">
      <template #extra>
        <n-space vertical align="center" :size="8">
          <span style="font-size: 12px; opacity: 0.65">群列表从协议端同步（需要在 AstrBot 里配置并启用 aiocqhttp 适配器）。</span>
          <n-button size="small" type="primary" :loading="syncing" @click="syncNow">立即同步</n-button>
        </n-space>
      </template>
    </n-empty>

    <template v-else>
      <n-data-table
        v-model:checked-row-keys="checked"
        :columns="columns"
        :data="rows"
        :loading="loading"
        :row-key="(row: GroupRow) => row.group_id"
        :bordered="false"
        :scroll-x="1350"
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

  <subject-drawer v-model:show="drawerShow" type="group" :id="drawerId" @changed="load" />
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
