<script setup lang="ts">
// 群成员级管控抽屉：列出某个群的成员，并对**个别人**单独设权限。
//
// 与「好友级」的区别（重要）：
//   · 好友级（scope=user）是跨群的：某个人在所有群 + 私聊都生效；
//   · 群成员级（scope=member，键是「群号:QQ」）只在**这个群**里生效，而且比
//     好友 / 等级 / 群规则都更具体 —— 所以能实现「整群禁止，只给某个人放行」。
//
// 成员列表来自本地缓存（协议端 get_group_member_list 很重），没同步过时给空态引导。
import { computed, h, ref, watch } from "vue";
import {
  NButton,
  NCard,
  NDataTable,
  NDrawer,
  NDrawerContent,
  NEmpty,
  NInput,
  NPagination,
  NSpace,
  NSpin,
  NTag,
  NTooltip,
  useMessage,
  type DataTableColumns,
} from "naive-ui";

import {
  apiGroupMembers,
  apiPost,
  apiSyncGroupMembers,
  type GroupMemberRow,
} from "../api";
import { dropAvatars, loadAvatars } from "../avatarStore";
import EffectSegment from "./EffectSegment.vue";
import SubjectAvatar from "./SubjectAvatar.vue";

const props = defineProps<{
  show: boolean;
  groupId: string;
  groupName?: string;
  platformId?: string;
}>();

const emit = defineEmits<{
  (e: "update:show", value: boolean): void;
  /** 成员权限变了（群列表不用刷新，但保持一致） */
  (e: "changed"): void;
}>();

const message = useMessage();
const loading = ref(false);
const syncing = ref(false);
const rows = ref<GroupMemberRow[]>([]);
const total = ref(0);
const page = ref(1);
const size = ref(200);
const keyword = ref("");
const checked = ref<string[]>([]);
const syncedAt = ref(0);

const showDrawer = computed({
  get: () => props.show,
  set: (v: boolean) => emit("update:show", v),
});

const roleMeta: Record<string, { text: string; type: "success" | "info" | "default" }> = {
  owner: { text: "群主", type: "success" },
  admin: { text: "管理员", type: "info" },
  member: { text: "成员", type: "default" },
};

function fmtTime(ts: number): string {
  return ts ? new Date(ts * 1000).toLocaleString() : "从未";
}

async function load() {
  if (!props.groupId) return;
  loading.value = true;
  try {
    const res = await apiGroupMembers(props.groupId, keyword.value, page.value, size.value);
    rows.value = res.rows || [];
    total.value = Number(res.total || 0);
    syncedAt.value = Number(res.synced_at || 0);
    loadAvatars("user", rows.value.map((r) => r.avatar_id || r.user_id));
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

/** 从协议端拉一次成员列表（可能较慢，需要协议端支持 get_group_member_list）。 */
async function syncMembers() {
  if (!props.groupId) return;
  syncing.value = true;
  try {
    const res = await apiSyncGroupMembers(props.groupId, props.platformId || "");
    dropAvatars("user", rows.value.map((r) => r.avatar_id || r.user_id));
    message.success(`已同步 ${res.count} 个成员`);
    page.value = 1;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e), { duration: 8000 });
  } finally {
    syncing.value = false;
  }
}

/** 写该成员在这个群里的权限（scope=member，只影响这个群）。 */
async function applyPolicy(
  items: { user_id: string; effect: string }[],
  feature: "llm" | "command",
) {
  if (!items.length) {
    message.warning("请先选择成员");
    return;
  }
  const label = feature === "command" ? "指令权限" : "LLM 权限";
  try {
    await apiPost("/policy", {
      items: items.map((i) => ({
        scope_type: "member",
        scope_id: `${props.groupId}:${i.user_id}`,
        effect: i.effect,
        feature,
      })),
    });
    message.success(`已更新 ${items.length} 个成员的${label}（仅本群生效）`);
    checked.value = [];
    await load();
    emit("changed");
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

const batchOptions = [
  {
    type: "group",
    label: "LLM 权限",
    key: "g-llm",
    children: [
      { label: "放行（本群）", key: "llm:allow" },
      { label: "禁止（本群）", key: "llm:deny" },
      { label: "恢复继承", key: "llm:inherit" },
    ],
  },
  {
    type: "group",
    label: "指令权限",
    key: "g-cmd",
    children: [
      { label: "放行（本群）", key: "cmd:allow" },
      { label: "禁止（本群）", key: "cmd:deny" },
      { label: "恢复继承", key: "cmd:inherit" },
    ],
  },
];

async function onBatch(key: string) {
  const [kind, effect] = String(key).split(":");
  if (!effect) return;
  await applyPolicy(
    checked.value.map((uid) => ({ user_id: uid, effect })),
    kind === "cmd" ? "command" : "llm",
  );
}

function search() {
  page.value = 1;
  load();
}

const columns: DataTableColumns<GroupMemberRow> = [
  {
    title: "成员",
    key: "display_name",
    minWidth: 200,
    render: (row) => {
      const meta = roleMeta[row.role] || roleMeta.member;
      return h("div", { style: "display:flex;align-items:center;gap:8px" }, [
        h(SubjectAvatar, { kind: "user", id: row.avatar_id || row.user_id, size: 28 }),
        h("div", { style: "line-height:1.3;min-width:0" }, [
          h("div", { style: "display:flex;align-items:center;gap:6px" }, [
            h("span", { style: "font-size:13px" }, row.display_name),
            h(NTag, { size: "tiny", bordered: false, type: meta.type }, { default: () => meta.text }),
          ]),
          row.card && row.nickname
            ? h("div", { style: "font-size:11.5px;opacity:.6" }, `昵称：${row.nickname}`)
            : null,
        ]),
      ]);
    },
  },
  { title: "QQ", key: "user_id", width: 120 },
  {
    title: () =>
      h(NTooltip, { trigger: "hover" }, {
        trigger: () => h("span", {}, "LLM 权限（本群）"),
        default: () => "只在这个群里生效：比好友 / 等级 / 群规则都更具体，可用来做「整群禁止 + 个别人放行」",
      }),
    key: "effect",
    width: 190,
    render: (row) =>
      h(EffectSegment, {
        effect: row.effect,
        onChange: (v: string) => applyPolicy([{ user_id: row.user_id, effect: v }], "llm"),
      }),
  },
  {
    title: () =>
      h(NTooltip, { trigger: "hover" }, {
        trigger: () => h("span", {}, "指令权限（本群）"),
        default: () => "禁止 = 他在这个群里用不了任何指令（只影响本群）",
      }),
    key: "effect_command",
    width: 190,
    render: (row) =>
      h(EffectSegment, {
        effect: row.effect_command,
        onChange: (v: string) => applyPolicy([{ user_id: row.user_id, effect: v }], "command"),
      }),
  },
  {
    title: "本群今日用量",
    key: "today_tokens",
    width: 110,
    render: (row) => (row.today_tokens ? `${(row.today_tokens / 1000).toFixed(1)}K` : "-"),
  },
];

watch(
  () => props.show,
  (v) => {
    if (v) {
      page.value = 1;
      keyword.value = "";
      checked.value = [];
      load();
    }
  },
);
</script>

<template>
  <n-drawer v-model:show="showDrawer" :width="920" placement="right">
    <n-drawer-content :title="`群成员：${props.groupName || props.groupId}`" closable>
      <n-space vertical :size="12">
        <n-card size="small">
          <n-space vertical :size="4" style="font-size: 13px; opacity: 0.82">
            <span>
              · 这里配的是「<b>某个人在这个群里</b>」的权限（<code>群号:QQ</code>），
              优先级<b>高于</b>好友权限、等级与群规则 ——
              所以「整群禁止，只给个别人放行」就靠这里。
            </span>
            <span>
              · 它<b>只影响这个群</b>：同一份权限不会带到别的群，也不影响私聊。
              跨群的统一设置请去「私聊」页切到「群聊」场景配。
            </span>
            <span v-if="!total" style="color: #f0a020">
              · 本地还没有这个群的成员缓存，点右上角「同步成员」从协议端拉一次
              （需要协议端支持 <code>get_group_member_list</code>）。
            </span>
          </n-space>
        </n-card>

        <n-space align="center" justify="space-between" :size="10" style="flex-wrap: wrap">
          <n-space align="center" :size="8">
            <n-input
              v-model:value="keyword"
              size="small"
              placeholder="QQ / 昵称 / 群名片"
              style="width: 180px"
              clearable
              @keyup.enter="search"
            />
            <n-button size="small" @click="search">搜索</n-button>
            <n-tag size="small" :bordered="false">共 {{ total }} 人</n-tag>
            <n-tag size="small" :bordered="false">上次同步：{{ fmtTime(syncedAt) }}</n-tag>
          </n-space>
          <n-button size="small" type="primary" :loading="syncing" ghost @click="syncMembers">
            同步成员
          </n-button>
        </n-space>

        <n-space v-if="checked.length" align="center" :size="8">
          <span style="font-size: 12px; opacity: 0.7">已选 {{ checked.length }} 人</span>
          <n-dropdown trigger="click" :options="batchOptions" @select="onBatch">
            <n-button size="small" type="primary" ghost>批量权限</n-button>
          </n-dropdown>
        </n-space>

        <n-spin :show="loading">
          <n-empty
            v-if="!loading && !total"
            description="暂无成员数据"
            style="padding: 40px 0"
          >
            <template #extra>
              <n-space vertical align="center" :size="8">
                <span style="font-size: 12px; opacity: 0.65">
                  成员列表从协议端同步（需要 aiocqhttp / OneBot 适配器在线）。
                </span>
                <n-button size="small" type="primary" :loading="syncing" @click="syncMembers">
                  立即同步
                </n-button>
              </n-space>
            </template>
          </n-empty>
          <template v-else>
            <n-data-table
              v-model:checked-row-keys="checked"
              :columns="columns"
              :data="rows"
              :row-key="(row: GroupMemberRow) => row.user_id"
              :bordered="false"
              :scroll-x="820"
              size="small"
              :max-height="520"
            />
            <n-space justify="end" style="margin-top: 12px">
              <n-pagination
                v-model:page="page"
                v-model:page-size="size"
                :item-count="total"
                :page-sizes="[200, 500, 1000]"
                show-size-picker
                @update:page="load"
                @update:page-size="search"
              />
            </n-space>
          </template>
        </n-spin>
      </n-space>
    </n-drawer-content>
  </n-drawer>
</template>
