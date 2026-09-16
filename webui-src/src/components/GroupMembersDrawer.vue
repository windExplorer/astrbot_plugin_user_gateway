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
  NInputNumber,
  NModal,
  NPagination,
  NProgress,
  NRadioButton,
  NRadioGroup,
  NSpace,
  NSpin,
  NTag,
  NTooltip,
  useMessage,
  type DataTableColumns,
} from "naive-ui";

import {
  apiGetQuota,
  apiGroupMembers,
  apiPost,
  apiResetQuota,
  apiSetQuota,
  apiSyncGroupMembers,
  type GroupMemberRow,
} from "../api";
import { useIsMobile } from "../responsive";

// 窄屏时成员抽屉占满屏宽（96%）
const isMobile = useIsMobile();
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

function fmtNum(n: number | undefined | null): string {
  const v = Number(n || 0);
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(2) + "M";
  if (v >= 1_000) return (v / 1_000).toFixed(1) + "K";
  return String(v);
}

const periodText = (p: string) => (p === "day" ? "每日" : p === "month" ? "每月" : "累计");
const PERIODS = ["day", "month", "total"] as const;

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
        // 优先用后端下发的 scope_id（群号:QQ），避免前端自己拼格式
        scope_id: i.scope_id || `${props.groupId}:${i.user_id}`,
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

// ------ 额度：给「这个人 + 这个群」单独设 token 限额 ------
// 判定时用的是**他在这个群里的用量**（member 维度），所以不会与别的群互相干扰。
const quotaShow = ref(false);
const quotaTargets = ref<string[]>([]);
const quotaSaving = ref(false);
const quotaMode = ref<"enforce" | "observe">("enforce");
// 三个周期各自填值：null = 不修改，0 = 明确「不限」，>0 = 设上限
const quotaForm = ref<{ day: number | null; month: number | null; total: number | null }>({
  day: null,
  month: null,
  total: null,
});
// 被点「清除」的周期 → 写成 null（删掉规则、恢复继承）
const quotaCleared = ref<string[]>([]);

const quotaTargetLabel = computed(() =>
  quotaTargets.value.length === 1
    ? `成员 ${quotaTargets.value[0]}`
    : `${quotaTargets.value.length} 个成员`,
);

function sidOf(uid: string): string {
  return `${props.groupId}:${uid}`;
}

function quotaPercent(row: GroupMemberRow): number {
  const q = row.quota;
  if (!q?.limit) return 0;
  return Math.min(100, Math.round(((q.used || 0) / q.limit) * 100));
}

/** 打开额度弹窗；``row`` 为空表示对勾选的成员批量设置。 */
async function openQuota(row?: GroupMemberRow) {
  quotaTargets.value = row ? [row.user_id] : [...checked.value];
  if (!quotaTargets.value.length) {
    message.warning("请先选择成员");
    return;
  }
  quotaForm.value = { day: null, month: null, total: null };
  quotaCleared.value = [];
  quotaMode.value = "enforce";
  quotaShow.value = true;
  if (row) {
    // 单个成员：把已配的额度读出来当初值
    try {
      const res = await apiGetQuota("member", sidOf(row.user_id));
      for (const it of res.items || []) {
        if (it.period === "day" || it.period === "month" || it.period === "total") {
          quotaForm.value[it.period] = Number(it.limit_tokens);
          quotaMode.value = (it.mode as "enforce" | "observe") || "enforce";
        }
      }
    } catch {
      /* 读不到就当没配过 */
    }
  }
}

function clearPeriod(p: "day" | "month" | "total") {
  quotaForm.value[p] = null;
  if (!quotaCleared.value.includes(p)) quotaCleared.value.push(p);
}

async function saveQuota() {
  const items: {
    scope_type: string;
    scope_id: string;
    period: string;
    limit_tokens: number | null;
    mode?: string;
  }[] = [];
  for (const uid of quotaTargets.value) {
    for (const p of ["day", "month", "total"] as const) {
      const sid = sidOf(uid);
      if (quotaCleared.value.includes(p)) {
        items.push({ scope_type: "member", scope_id: sid, period: p, limit_tokens: null });
        continue;
      }
      const v = quotaForm.value[p];
      if (v === null || v === undefined) continue; // 留空 = 不动这个周期
      items.push({
        scope_type: "member",
        scope_id: sid,
        period: p,
        limit_tokens: Number(v),
        mode: quotaMode.value,
      });
    }
  }
  if (!items.length) {
    message.warning("没有要修改的周期（留空表示不修改）");
    return;
  }
  quotaSaving.value = true;
  try {
    await apiSetQuota(items);
    message.success(`已更新 ${quotaTargets.value.length} 个成员的额度（仅本群生效）`);
    quotaShow.value = false;
    await load();
    emit("changed");
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    quotaSaving.value = false;
  }
}

async function resetUsage() {
  if (!quotaTargets.value.length) return;
  quotaSaving.value = true;
  try {
    for (const uid of quotaTargets.value) {
      await apiResetQuota("member", sidOf(uid));
    }
    message.success("已清零这些成员在本群的用量");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    quotaSaving.value = false;
  }
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
    title: () =>
      h(NTooltip, { trigger: "hover" }, {
        trigger: () => h("span", {}, "额度（本群）"),
        default: () =>
          "给这个人单独设 token 限额（本群专属）：判定用的是他在这个群里的用量，" +
          "不影响别的群，也不受「好友级额度」的跨群合计干扰",
      }),
    key: "quota",
    width: 200,
    render: (row) => {
      const q = row.quota;
      if (!q || !q.limit) {
        return h("div", { style: "display:flex;align-items:center;gap:6px" }, [
          h(NTag, { size: "tiny", bordered: false }, { default: () => "不限量" }),
          h(NButton, { size: "tiny", quaternary: true, onClick: () => openQuota(row) }, { default: () => "设额度" }),
        ]);
      }
      return h("div", { style: "min-width:170px" }, [
        h(
          "div",
          { style: "font-size:12px;margin-bottom:2px;display:flex;justify-content:space-between;gap:8px" },
          [
            h("span", {}, `${fmtNum(q.used)} / ${fmtNum(q.limit)}`),
            h(NTooltip, { trigger: "hover" }, {
              trigger: () =>
                h(
                  NTag,
                  {
                    size: "tiny",
                    bordered: false,
                    type: q.mode === "observe" ? "warning" : "default",
                  },
                  { default: () => q.layer_label },
                ),
              default: () =>
                `生效档位：${q.layer_label}｜周期：${periodText(q.period)}` +
                `${q.mode === "observe" ? "｜观察模式（只记账不拦截）" : ""}`,
            }),
          ],
        ),
        h(NProgress, {
          type: "line",
          percentage: quotaPercent(row),
          height: 6,
          showIndicator: false,
          status: q.exceeded ? "error" : undefined,
        }),
        h(
          NButton,
          { size: "tiny", quaternary: true, style: "margin-top:2px", onClick: () => openQuota(row) },
          { default: () => "设额度" },
        ),
      ]);
    },
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
  <n-drawer v-model:show="showDrawer" :width="isMobile ? '96%' : 920" placement="right">
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
          <n-button size="small" ghost @click="openQuota()">批量额度</n-button>
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

    <n-modal
      v-model:show="quotaShow"
      preset="card"
      :title="`设置额度：${quotaTargetLabel}（仅本群生效）`"
      style="width: 640px; max-width: 94vw"
    >
      <n-space vertical :size="12">
        <span style="font-size: 12px; opacity: 0.7; line-height: 1.6">
          留空 = <b>不修改</b>该周期；填 <b>0</b> = 明确「<b>不限</b>」（会占住档位，更粗的额度不再生效）；
          填数字 = 设上限。<br />
          判定用的是<b>他在这个群里的用量</b>，所以不影响别的群、也不受「好友级额度」的跨群合计干扰。
        </span>
        <n-space v-for="p in PERIODS" :key="p" align="center" :size="8">
          <span style="width: 44px; font-size: 13px">{{ periodText(p) }}</span>
          <n-input-number
            v-model:value="quotaForm[p]"
            :min="0"
            :show-button="false"
            clearable
            placeholder="留空 = 不修改"
            style="width: 190px"
          >
            <template #suffix>token</template>
          </n-input-number>
          <n-button size="tiny" quaternary @click="clearPeriod(p)">清除该周期额度</n-button>
        </n-space>
        <n-space align="center" :size="10">
          <span style="font-size: 13px">超限处理</span>
          <n-radio-group v-model:value="quotaMode" size="small">
            <n-radio-button value="enforce">拦截</n-radio-button>
            <n-radio-button value="observe">只观察</n-radio-button>
          </n-radio-group>
        </n-space>
        <n-space justify="space-between" align="center">
          <n-button size="small" quaternary @click="resetUsage">清零本群用量</n-button>
          <n-space :size="8">
            <n-button size="small" @click="quotaShow = false">取消</n-button>
            <n-button size="small" type="primary" :loading="quotaSaving" @click="saveQuota">保存</n-button>
          </n-space>
        </n-space>
      </n-space>
    </n-modal>
  </n-drawer>
</template>
