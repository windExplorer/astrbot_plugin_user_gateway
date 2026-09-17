<script setup lang="ts">
// 私聊：QQ 好友列表 + 等级 / LLM 权限 / 生效额度 / 最后回复，支持逐个与批量管控。
//
// 「场景」：好友的权限分**私聊**与**群聊**两个维度（同一个人可以「私聊禁用、群里照用」），
// 顶部切换器决定权限列读写哪一套；群专属 / 群等级规则则天然只属于群聊场景。
import { computed, h, onMounted, onUnmounted, ref } from "vue";

import { useIsMobile } from "../responsive";
import {
  NButton,
  NCard,
  NDataTable,
  NDropdown,
  NEmpty,
  NInput,
  NPagination,
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
import PermissionHelp from "../components/PermissionHelp.vue";
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
const cmdFilter = ref("");
const sort = ref("last");
// 等级筛选："" = 全部，「0」= 未分组，其余为等级 id
const levelFilter = ref("");
// 作用场景：好友的权限分「私聊」「群聊」两个维度，切换后列表里的权限列读写对应的那一套
const scene = ref<"private" | "group">("private");
const sceneLabel = computed(() => (scene.value === "group" ? "群聊" : "私聊"));
// 移动端：卡片列表 + 可折叠筛选面板（桌面端仍是完整表格）
const isMobile = useIsMobile();
const mobileFilters = ref(false);
const activeFilterCount = computed(
  () => [effectFilter.value, cmdFilter.value, levelFilter.value, keyword.value.trim()].filter(Boolean).length,
);
function fmtTokens(n: number | undefined | null): string {
  return n ? `${(n / 1000).toFixed(1)}K` : "0";
}
/** 卡片上的权限徽标：只展示，不编辑（编辑进详情抽屉）。 */
function effectMeta(v: string | undefined | null) {
  const e = v || "inherit";
  return {
    type: e === "deny" ? ("error" as const) : e === "allow" ? ("success" as const) : ("default" as const),
    text: e === "deny" ? "禁止" : e === "allow" ? "放行" : "继承",
  };
}
/** 移动端筛选面板的「搜索」：应用条件并收起面板。 */
function applyFilters() {
  search();
  mobileFilters.value = false;
}
function openDetail(uin: string) {
  drawerId.value = uin;
  drawerShow.value = true;
}
/** 等级 id → 名称（卡片展示用；0/null = 未分组）。 */
function levelNameOf(id: number | null | undefined): string {
  if (!id) return "未分组";
  const lv = levels.value.find((l) => l.id === id);
  return lv ? lv.name : String(id);
}
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

// 默认按「最近回复」倒序：最关心的是「谁最近还在用」，从没回复过的自然沉底。
const sortOptions = [
  { label: "最近回复", value: "last" },
  { label: "最近同步", value: "active" },
  { label: "今日用量", value: "usage" },
  { label: "昵称", value: "name" },
  { label: "QQ 号", value: "qq" },
  { label: "等级", value: "level" },
];

// 等级筛选项：数量跟着等级变化实时刷新（等级管理里改完回来就是新的）
const levelFilterOptions = computed(() => [
  { label: "全部等级", value: "" },
  { label: "未分组", value: "0" },
  ...levels.value.map((l) => ({ label: `${l.name}（${l.members}）`, value: String(l.id) })),
]);

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
        `&effect=${encodeURIComponent(effectFilter.value)}` +
        `&effect_command=${encodeURIComponent(cmdFilter.value)}` +
        `&level_id=${encodeURIComponent(levelFilter.value)}` +
        `&scene=${scene.value}` +
        `&q=${encodeURIComponent(keyword.value)}`,
    );
    rows.value = res.rows || [];
    total.value = res.total || 0;
    // 顺手刷新等级（下拉里的数量要跟着归级变化实时更新，不能只在进页面时取一次）
    loadLevels();
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

/**
 * 写权限规则。``feature`` 决定改的是哪一类：
 * ``"llm"`` = LLM 对话权限，``"command"`` = 指令权限（对象级总开关）。
 */
async function applyPolicy(items: { scope_id: string; effect: string }[], feature = "llm", silent = false) {
  if (!items.length) {
    message.warning("请先选择好友");
    return;
  }
  const label = feature === "command" ? "指令权限" : "LLM 权限";
  try {
    await apiPost("/policy", {
      // scene：只改「当前作用场景」那一套规则（另一个场景不受影响）
      items: items.map((i) => ({
        scope_type: "user",
        scope_id: i.scope_id,
        effect: i.effect,
        feature,
        scene: scene.value,
      })),
    });
    if (!silent) message.success(`已更新 ${items.length} 个好友的${label}`);
    checked.value = [];
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

function applyEffect(items: { scope_id: string; effect: string }[], silent = false) {
  return applyPolicy(items, "llm", silent);
}

async function applyLevel(
  items: { scope_id: string; level_id: number | null }[],
  levelScene: "private" | "group" = "private",
) {
  if (!items.length) {
    message.warning("请先选择好友");
    return;
  }
  try {
    await apiSetSubjectLevel(
      items.map((i) => ({
        scope_type: "user",
        scope_id: i.scope_id,
        level_id: i.level_id,
        scene: levelScene,
      })),
    );
    message.success(
      `已更新 ${items.length} 个好友的${levelScene === "group" ? "群聊" : "私聊"}等级`,
    );
    checked.value = [];
    batchLevelId.value = null;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

// 批量下拉：LLM 权限与指令权限是两套独立规则，分开列，避免点错。
// 分组标题带上当前场景，提醒「这次改的是哪一套」。
const batchOptions = computed(() => [
  {
    type: "group",
    label: `LLM 权限（${sceneLabel.value}）`,
    key: "g-llm",
    children: [
      { label: "放行", key: "llm:allow" },
      { label: "禁止", key: "llm:deny" },
      { label: "恢复继承", key: "llm:inherit" },
    ],
  },
  {
    type: "group",
    label: `指令权限（${sceneLabel.value}）`,
    key: "g-cmd",
    children: [
      { label: "放行", key: "cmd:allow" },
      { label: "禁止（不能用任何指令）", key: "cmd:deny" },
      { label: "恢复继承", key: "cmd:inherit" },
    ],
  },
]);

async function onBatch(key: string) {
  const [kind, effect] = String(key).split(":");
  if (!effect) return;
  await applyPolicy(
    checked.value.map((id) => ({ scope_id: id, effect })),
    kind === "cmd" ? "command" : "llm",
  );
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
    title: "私聊等级",
    key: "level_id",
    width: 130,
    render: (row) =>
      h(NSelect, {
        size: "tiny",
        value: row.level_id_base || 0,
        options: levelOptions.value,
        consistentMenuWidth: false,
        onUpdateValue: (v: number) =>
          applyLevel([{ scope_id: row.uin, level_id: v || null }], "private"),
      }),
  },
  {
    title: "群聊等级",
    key: "level_id_group",
    width: 130,
    render: (row) =>
      h(NSelect, {
        size: "tiny",
        value: row.level_id_group ?? -1,
        options: [
          { label: "跟随私聊", value: -1 },
          ...levelOptions.value,
        ],
        consistentMenuWidth: false,
        onUpdateValue: (v: number) =>
          applyLevel(
            [{ scope_id: row.uin, level_id: v === -1 ? null : v }],
            "group",
          ),
      }),
  },
  {
    // 标题是函数 → 切换场景时跟着变（naive-ui 会把函数当渲染函数，读 scene.value 即自动响应）
    title: () => `LLM 权限（${sceneLabel.value}）`,
    key: "effect",
    width: 195,
    render: (row) => h(EffectSegment, { effect: row.effect, onChange: (v: string) => applyEffect([{ scope_id: row.uin, effect: v }]) }),
  },
  {
    title: () => `指令权限（${sceneLabel.value}）`,
    key: "effect_command",
    width: 195,
    render: (row) =>
      h(NTooltip, { trigger: "hover", placement: "top" }, {
        trigger: () =>
          h(EffectSegment, {
            effect: row.effect_command || "inherit",
            onChange: (v: string) => applyPolicy([{ scope_id: row.uin, effect: v }], "command"),
          }),
        default: () => "指令权限：禁止 = 这个人用不了任何指令（不含等级与单条指令的细则）",
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
    title: "最后回复",
    key: "last_bot_ts",
    width: 140,
    render: (row) => {
      if (!row.last_bot_ts) {
        // 「今日用量」按人头聚合（含他在群里的消费），而「最后回复」只记**私聊**——
        // bot 在群里的回复记在群名下。只在群里聊过的人就会出现「有用量、无回复记录」。
        if (row.today_tokens) {
          return h(NTooltip, { trigger: "hover" }, {
            trigger: () => h("span", { style: "opacity:.4" }, "无私聊记录"),
            default: () =>
              "bot 没有私聊过这个人：今日用量来自群聊（群里的回复记在群上，\n不记到个人）。私聊过之后这里才会出现记录。",
          });
        }
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

// 静默刷新定时器（见 onMounted）
let refreshTimer: ReturnType<typeof setInterval> | null = null;

onMounted(async () => {
  await loadLevels();
  await load();
  // 列表数据只在进页时加载一次，「最后回复 / 今日用量」会随实际对话过期——
  // 页面可见时每 30s 静默刷新一轮（不可见时跳过，避免无谓请求）
  refreshTimer = setInterval(() => {
    if (document.visibilityState === "visible") load();
  }, 30_000);
});
onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer);
});
</script>

<template>
  <permission-help kind="user" />

  <n-card size="small" style="margin-top: 12px">
    <template #header>
      <n-space align="center" :size="10">
        <span>私聊好友</span>
        <n-tag size="small" :bordered="false">{{ total }} 个</n-tag>
      </n-space>
    </template>
    <template #header-extra>
      <!-- 移动端：只留「筛选」入口（带生效条件数），控件收进下方可折叠面板 -->
      <n-button v-if="isMobile" size="small" @click="mobileFilters = !mobileFilters">
        筛选{{ activeFilterCount ? `（${activeFilterCount}）` : "" }}
      </n-button>
      <n-space v-else :size="8" align="center">
        <n-radio-group v-model:value="scene" size="small" @update:value="search">
          <n-radio-button value="private">私聊</n-radio-button>
          <n-radio-button value="group">群聊</n-radio-button>
        </n-radio-group>
        <n-input
          v-model:value="keyword"
          size="small"
          placeholder="QQ 号 / 昵称 / 备注"
          style="width: 180px"
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
        <n-select
          v-model:value="levelFilter"
          size="small"
          style="width: 200px"
          :options="levelFilterOptions"
          @update:value="search"
        />
        <n-button size="small" @click="search">搜索</n-button>
        <n-button size="small" :loading="syncing" @click="syncNow">同步列表</n-button>
        <n-button size="small" :loading="refreshingAvatars" @click="refreshAvatars">更新头像</n-button>
      </n-space>
    </template>

    <!-- 移动端筛选面板：控件纵向铺满，不挤在一行 -->
    <div v-if="isMobile && mobileFilters" class="m-filters">
      <n-radio-group v-model:value="scene" size="small" @update:value="search">
        <n-radio-button value="private">私聊</n-radio-button>
        <n-radio-button value="group">群聊</n-radio-button>
      </n-radio-group>
      <n-input
        v-model:value="keyword"
        size="small"
        placeholder="QQ 号 / 昵称 / 备注"
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
      <n-select v-model:value="sort" size="small" :options="sortOptions" @update:value="search" />
      <n-select
        v-model:value="levelFilter"
        size="small"
        :options="levelFilterOptions"
        @update:value="search"
      />
      <n-space :size="8">
        <n-button size="small" type="primary" ghost @click="applyFilters">搜索</n-button>
        <n-button size="small" :loading="syncing" @click="syncNow">同步列表</n-button>
        <n-button size="small" :loading="refreshingAvatars" @click="refreshAvatars">更新头像</n-button>
      </n-space>
    </div>

    <!-- 批量操作条：选中行后出现（移动端卡片不支持勾选，批量操作在桌面用） -->
    <n-space v-if="checked.length && !isMobile" align="center" :size="8" style="margin-bottom: 10px">
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
      <!-- 移动端：卡片列表，只展示关键信息，点卡片进详情 -->
      <div v-if="isMobile" class="m-list">
        <div v-for="row in rows" :key="row.uin" class="m-card" @click="openDetail(row.uin)">
          <div class="m-head">
            <subject-avatar kind="user" :id="row.uin" :name="row.display_name" :size="40" />
            <div class="m-title">
              <div class="m-name">{{ row.display_name }}</div>
              <div class="m-sub">
                {{ row.uin }} · 私聊 {{ levelNameOf(row.level_id_base) }}
                <template v-if="row.level_id_group"> · 群聊 {{ levelNameOf(row.level_id_group) }}</template>
              </div>
            </div>
            <span class="m-arrow">›</span>
          </div>
          <div class="m-row">
            <n-tag size="small" :bordered="false" :type="effectMeta(row.effect).type">
              LLM：{{ effectMeta(row.effect).text }}
            </n-tag>
            <n-tag size="small" :bordered="false" :type="effectMeta(row.effect_command).type">
              指令：{{ effectMeta(row.effect_command).text }}
            </n-tag>
          </div>
          <div class="m-meta">
            <span>今日 {{ fmtTokens(row.today_tokens) }}</span>
            <span v-if="row.last_bot_ts" class="m-reply">
              {{ kindMeta(row.last_bot_kind).text }} · {{ relTime(row.last_bot_ts) }}
            </span>
            <span v-else style="opacity: 0.5">无回复记录</span>
          </div>
        </div>
      </div>
      <n-data-table
        v-else
        v-model:checked-row-keys="checked"
        :columns="columns"
        :data="rows"
        :loading="loading"
        :row-key="(row: FriendRow) => row.uin"
        :bordered="false"
        :scroll-x="1420"
        size="small"
      />
      <n-space justify="end" style="margin-top: 12px">
        <n-pagination
          v-model:page="page"
          v-model:page-size="size"
          :item-count="total"
          :page-sizes="[20, 50, 100]"
          :show-size-picker="!isMobile"
          simple
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

/* ---- 移动端卡片列表 ---- */
.m-filters {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}
.m-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.m-card {
  border: 1px solid rgba(128, 128, 128, 0.25);
  border-radius: 10px;
  padding: 10px 12px;
  cursor: pointer;
  transition: background-color 0.15s;
}
.m-card:active {
  background: rgba(128, 128, 128, 0.12);
}
.m-head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.m-title {
  flex: 1;
  min-width: 0;
  line-height: 1.35;
}
.m-name {
  font-size: 14px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.m-sub {
  font-size: 12px;
  opacity: 0.6;
}
.m-arrow {
  font-size: 20px;
  opacity: 0.35;
  line-height: 1;
}
.m-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
}
.m-label {
  font-size: 12px;
  opacity: 0.6;
  width: 26px;
}
.m-meta {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 6px;
  font-size: 12px;
  opacity: 0.75;
}
.m-reply {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 60%;
}
</style>
