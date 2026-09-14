<script setup lang="ts">
// 私聊：QQ 好友列表 + 等级 / LLM 权限 / 生效额度 / 最后回复，支持逐个与批量管控。
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
  type FriendRow,
  type LevelRow,
  type Paged,
} from "../api";
import { dropAvatars, loadAvatars } from "../avatarStore";
import EffectSegment from "../components/EffectSegment.vue";
import SubjectAvatar from "../components/SubjectAvatar.vue";
import SubjectDrawer from "../components/SubjectDrawer.vue";

const message = useMessage();
const loading = ref(false);
const syncing = ref(false);
const refreshingAvatars = ref(false);
const rows = ref<FriendRow[]>([]);
const total = ref(0);
const page = ref(1);
const size = ref(50);
const keyword = ref("");
const effectFilter = ref("");
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
  { label: "昵称", value: "name" },
  { label: "QQ 号", value: "qq" },
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

/** 相对时间：列表上一眼能看出「多久之前 bot 还回过话」。 */
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
    const res = await apiLevels("user");
    levels.value = res.items || [];
  } catch {
    /* 等级加载失败不阻塞列表 */
  }
}

async function load() {
  loading.value = true;
  try {
    const res = await apiGet<Paged<FriendRow>>(
      `/friends?page=${page.value}&size=${size.value}&sort=${sort.value}` +
        `&effect=${encodeURIComponent(effectFilter.value)}&q=${encodeURIComponent(keyword.value)}`,
    );
    rows.value = res.rows || [];
    total.value = res.total || 0;
    // 当前页可见行的头像按需抓取（后端有缓存就直接给，没有才去 CDN 拉）
    loadAvatars("user", rows.value.map((r) => r.avatar_id || r.uin));
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

/** 强制重取当前好友的头像（换了头像之后用）。 */
async function refreshAvatars() {
  const ids = rows.value.map((r) => r.avatar_id || r.uin).filter(Boolean);
  if (!ids.length) return;
  refreshingAvatars.value = true;
  try {
    const res = await apiRefreshAvatars("user", ids);
    dropAvatars("user", ids);
    await load();
    message.success(`头像已更新：成功 ${res.refreshed} / 失败 ${res.failed}`);
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

async function applyEffect(items: { scope_id: string; effect: string }[], silent = false) {
  if (!items.length) {
    message.warning("请先选择好友");
    return;
  }
  try {
    await apiPost("/policy", {
      items: items.map((i) => ({ scope_type: "user", scope_id: i.scope_id, effect: i.effect, feature: "llm" })),
    });
    if (!silent) message.success(`已更新 ${items.length} 个好友的权限`);
    checked.value = [];
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function applyLevel(items: { scope_id: string; level_id: number | null }[]) {
  if (!items.length) {
    message.warning("请先选择好友");
    return;
  }
  try {
    await apiSetSubjectLevel(items.map((i) => ({ scope_type: "user", scope_id: i.scope_id, level_id: i.level_id })));
    message.success(`已更新 ${items.length} 个好友的等级`);
    checked.value = [];
    batchLevelId.value = null;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

const batchOptions = [
  { label: "批量放行", key: "allow" },
  { label: "批量禁止", key: "deny" },
  { label: "批量恢复继承", key: "inherit" },
];

async function onBatch(key: string) {
  await applyEffect(checked.value.map((id) => ({ scope_id: id, effect: key })));
}

function quotaPercent(row: FriendRow): number {
  if (!row.quota?.limit) return 0;
  return Math.min(100, Math.round(((row.quota.used || 0) / row.quota.limit) * 100));
}

const columns: DataTableColumns<FriendRow> = [
  { type: "selection" },
  {
    title: "好友",
    key: "display_name",
    minWidth: 190,
    render: (row) =>
      h(
        "div",
        {
          style: "display:flex;align-items:center;gap:10px;cursor:pointer",
          title: "点击查看详情",
          onClick: () => {
            drawerId.value = row.uin;
            drawerShow.value = true;
          },
        },
        [
          h(SubjectAvatar, { kind: "user", id: row.uin, name: row.display_name }),
          h("div", { style: "line-height:1.3" }, [
            h("div", { style: "font-weight:500" }, row.display_name || row.uin),
            h(
              "div",
              { style: "font-size:12px;opacity:.65" },
              `${row.uin}${row.remark ? " · 备注：" + row.remark : ""}`,
            ),
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
        onUpdateValue: (v: number) => applyLevel([{ scope_id: row.uin, level_id: v || null }]),
      }),
  },
  {
    title: "LLM 权限",
    key: "effect",
    width: 195,
    render: (row) => h(EffectSegment, { effect: row.effect, onChange: (v: string) => applyEffect([{ scope_id: row.uin, effect: v }]) }),
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
      h(NButton, {
        size: "tiny",
        quaternary: true,
        onClick: () => {
          drawerId.value = row.uin;
          drawerShow.value = true;
        },
      }, { default: () => "详情" }),
  },
];

onMounted(async () => {
  await loadLevels();
  await load();
});
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
          style="width: 180px"
          clearable
          @keyup.enter="search"
        />
        <select v-model="effectFilter" class="plain-select" @change="search">
          <option value="">全部权限</option>
          <option value="allow">放行</option>
          <option value="deny">禁止</option>
          <option value="inherit">继承</option>
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

    <!-- 批量操作条：选中行后出现 -->
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

    <n-empty
      v-if="!loading && !total"
      description="还没有好友数据"
      style="padding: 40px 0"
    >
      <template #extra>
        <n-space vertical align="center" :size="8">
          <span style="font-size: 12px; opacity: 0.65">
            好友列表从协议端同步（需要在 AstrBot 里配置并启用 aiocqhttp 适配器）。
          </span>
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
        :row-key="(row: FriendRow) => row.uin"
        :bordered="false"
        :scroll-x="1080"
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

  <subject-drawer v-model:show="drawerShow" type="user" :id="drawerId" @changed="load" />
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
