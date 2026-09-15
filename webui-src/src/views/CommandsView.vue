<script setup lang="ts">
// 指令：按指令名管控「全局 / 好友 / 群」的可用性（默认放行，可改成白名单模式）。
//
// 拦截机制（后端）：本插件注册了一个「所有消息 + 高优先级」的处理器，
// AstrBot 的 process 阶段按优先级依次执行 handler 且逐个检查 is_stopped()，
// 所以在这里 stop_event() 就能让后面的指令 handler 不执行。
import { computed, h, onMounted, ref } from "vue";
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NInput,
  NModal,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpace,
  NSpin,
  NTag,
  NTooltip,
  useMessage,
  type DataTableColumns,
} from "naive-ui";

import {
  apiCommandMatrix,
  apiCommands,
  apiGet,
  apiGroupMembers,
  apiLevels,
  apiPruneCommands,
  apiSetPolicy,
  type CommandMatrix,
  type CommandMatrixRow,
  type CommandRow,
} from "../api";
import EffectSegment from "../components/EffectSegment.vue";

const message = useMessage();
const loading = ref(false);
const saving = ref(false);
const pruning = ref(false);
const items = ref<CommandRow[]>([]);
const defaultEffect = ref<"allow" | "deny">("allow");
const guardEnabled = ref(true);
const priority = ref(1000);
const stale = ref<string[]>([]);
const keyword = ref("");

// 例外规则弹窗
const showRules = ref(false);
const current = ref<CommandRow | null>(null);
const adding = ref(false);
const newRule = ref({
  scope_type: "user" as "user" | "group",
  scope_id: "",
  effect: "deny" as "allow" | "deny",
});

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  if (!kw) return items.value;
  return items.value.filter((it) =>
    [it.name, it.desc, it.plugin, ...(it.aliases || [])]
      .join(" ")
      .toLowerCase()
      .includes(kw),
  );
});

function featureOf(name: string): string {
  return `command:${name}`;
}

async function load() {
  loading.value = true;
  try {
    const res = await apiCommands();
    items.value = res.items || [];
    defaultEffect.value = res.default_effect || "allow";
    guardEnabled.value = res.enabled !== false;
    priority.value = Number(res.priority || 1000);
    stale.value = res.stale || [];
    // 弹窗里同步最新数据
    if (current.value) {
      current.value = items.value.find((it) => it.name === current.value?.name) || null;
    }
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

async function setGlobal(row: CommandRow, effect: string) {
  saving.value = true;
  try {
    await apiSetPolicy([
      { scope_type: "global", scope_id: "*", effect: effect as "allow" | "deny" | "inherit", feature: featureOf(row.name) },
    ]);
    const tip =
      effect === "inherit"
        ? `「${row.name}」已恢复默认策略`
        : effect === "deny"
          ? `已全局禁用「${row.name}」`
          : `已全局放行「${row.name}」`;
    message.success(tip);
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

function openRules(row: CommandRow) {
  current.value = row;
  newRule.value = { scope_type: "user", scope_id: "", effect: "deny" };
  showRules.value = true;
}

async function addRule() {
  const row = current.value;
  if (!row) return;
  const sid = newRule.value.scope_id.trim();
  if (!sid) {
    message.warning(newRule.value.scope_type === "user" ? "请填写 QQ 号" : "请填写群号");
    return;
  }
  adding.value = true;
  try {
    await apiSetPolicy([
      {
        scope_type: newRule.value.scope_type,
        scope_id: sid,
        effect: newRule.value.effect,
        feature: featureOf(row.name),
      },
    ]);
    message.success("已添加例外规则");
    newRule.value.scope_id = "";
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    adding.value = false;
  }
}

async function removeRule(row: CommandRow, scopeType: "user" | "group", scopeId: string) {
  try {
    await apiSetPolicy([
      { scope_type: scopeType, scope_id: scopeId, effect: "inherit", feature: featureOf(row.name) },
    ]);
    message.success("已删除该条规则");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function prune() {
  pruning.value = true;
  try {
    const res = await apiPruneCommands();
    message.success(`已清理 ${res.deleted} 条失效规则`);
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    pruning.value = false;
  }
}

/** 规则里带的场景标记：只对某个场景生效时要写出来，否则会被误读成「全场生效」。 */
function sceneTag(scene?: string): string {
  if (scene === "private") return "（仅私聊）";
  if (scene === "group") return "（仅群聊）";
  return "";
}

/** 规则作用对象的文字：好友 / 群 / 群成员（``群号:QQ`` 得翻译一下才看得懂）。 */
function scopeLabel(r: { scope_type: string; scope_id: string }): string {
  if (r.scope_type === "member") {
    const [gid, uid] = String(r.scope_id || "").split(":");
    return `群 ${gid} 的成员 ${uid}`;
  }
  return `${r.scope_type === "user" ? "好友" : "群"} ${r.scope_id}`;
}

function ruleSummary(row: CommandRow): string {
  const rules = row.rules || [];
  if (!rules.length) return "";
  const text = rules
    .slice(0, 2)
    .map((r) => `${scopeLabel(r)}${sceneTag(r.scene)} ${r.effect === "deny" ? "禁止" : "放行"}`)
    .join("；");
  return rules.length > 2 ? `${text} 等 ${rules.length} 条` : text;
}

// ------------------------------------------------------------------ //
// 对象体检：某对象 × 所有指令的可用性矩阵
//
// 「这道指令他到底能不能用、结论是哪一层给的」以前只能靠一条条点开规则去推；
// 这里直接把整个结论摊平：选一个对象 → 列出所有指令 + 结论 + 来源 + 该对象的显式规则。
// ------------------------------------------------------------------ //
const mxType = ref<"user" | "group" | "member" | "level" | "global">("user");
const mxScene = ref<"private" | "group">("private");
const mxId = ref("");
const mxGroupId = ref(""); // 群成员模式：先选群
const mxLoading = ref(false);
const mxRows = ref<CommandMatrixRow[]>([]);
const mxSummary = ref<CommandMatrix["summary"] | null>(null);
const mxEditable = ref(false);
const mxLevelKind = ref("");
const mxObjects = ref<{ label: string; value: string }[]>([]);
const mxGroups = ref<{ label: string; value: string }[]>([]);

const mxTypeOptions = [
  { label: "好友", value: "user" },
  { label: "群聊", value: "group" },
  { label: "群成员", value: "member" },
  { label: "等级", value: "level" },
  { label: "全局", value: "global" },
];

const mxSceneOptions = [
  { label: "私聊场景", value: "private" },
  { label: "群聊场景", value: "group" },
];

/** 当前模式下真正提交给后端的 scope_type / scope_id。 */
function mxScope(): { scopeType: string; scopeId: string; scene: "private" | "group" } {
  const scene: "private" | "group" =
    mxType.value === "group" || mxType.value === "member" ? "group" : mxScene.value;
  return {
    scopeType: mxType.value,
    // 群成员的选项值就是后端给的「群号:QQ」，其它模式直接用对象 id
    scopeId: mxId.value,
    scene,
  };
}

async function loadMatrixObjects() {
  mxId.value = "";
  mxRows.value = [];
  mxSummary.value = null;
  try {
    if (mxType.value === "user") {
      const res = await apiGet<{ rows: any[] }>("/friends?page=1&size=500&sort=last");
      mxObjects.value = (res.rows || []).map((r) => ({
        label: `${r.display_name}（${r.uin}）`,
        value: String(r.uin),
      }));
    } else if (mxType.value === "group" || mxType.value === "member") {
      const res = await apiGet<{ rows: any[] }>("/groups?page=1&size=500&sort=last");
      const opts = (res.rows || []).map((r) => ({
        label: `${r.name || r.group_id}（${r.group_id}）`,
        value: String(r.group_id),
      }));
      mxGroups.value = opts;
      mxObjects.value = mxType.value === "group" ? opts : [];
    } else if (mxType.value === "level") {
      const res = await apiLevels();
      mxObjects.value = (res.items || []).map((lv) => ({
        label: `${lv.name}（${lv.kind === "group" ? "群聊" : "好友"}等级 · ${lv.members} 人在此档）`,
        value: String(lv.id),
      }));
    } else {
      mxObjects.value = [{ label: "全局（所有对象）", value: "*" }];
      mxId.value = "*";
      await loadMatrix();
    }
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function loadMatrixMembers(groupId: string) {
  mxObjects.value = [];
  mxId.value = "";
  if (!groupId) return;
  try {
    const res = await apiGroupMembers(groupId, "", 1, 1000);
    mxObjects.value = (res.rows || []).map((m) => ({
      label: `${m.display_name}（${m.user_id}）`,
      // 直接用后端给的规则 scope_id（群号:QQ），避免自己拼格式
      value: m.scope_id || `${groupId}:${m.user_id}`,
    }));
    if (!res.rows?.length) {
      message.warning("这个群还没有成员缓存，先到「群聊」页点该行的「同步成员」");
    }
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function loadMatrix() {
  if (!mxId.value) return;
  const { scopeType, scopeId, scene } = mxScope();
  mxLoading.value = true;
  try {
    const res = await apiCommandMatrix(scopeType, scopeId, scene);
    mxRows.value = res.rows || [];
    mxSummary.value = res.summary || null;
    mxEditable.value = !!res.editable;
    mxLevelKind.value = res.level_kind || "";
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    mxLoading.value = false;
  }
}

/** 给该对象设「这条指令」的显式规则（写 feature=command:<指令名>）。 */
async function setMatrixRule(row: CommandMatrixRow, effect: string) {
  if (mxType.value === "level") return; // 等级层不支持单条指令规则
  const { scopeType, scopeId, scene } = mxScope();
  try {
    await apiSetPolicy([
      {
        scope_type: scopeType as any,
        scope_id: scopeId,
        effect: effect as any,
        feature: featureOf(row.name),
        scene: scopeType === "user" ? scene : "",
      },
    ]);
    message.success(
      `「${row.name}」已设为${effect === "allow" ? "放行" : effect === "deny" ? "禁止" : "继承"}`,
    );
    await loadMatrix();
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

const mxColumns: DataTableColumns<CommandMatrixRow> = [
  {
    title: "指令",
    key: "name",
    minWidth: 170,
    render: (row) =>
      h("div", { style: "line-height:1.35" }, [
        h("div", { style: "font-weight:500" }, [
          row.name,
          row.is_group
            ? h(NTag, { size: "tiny", bordered: false, style: "margin-left:6px" }, { default: () => "指令组" })
            : null,
        ]),
        row.aliases?.length
          ? h("div", { style: "font-size:12px;opacity:.65" }, `别名：${row.aliases.join(" / ")}`)
          : null,
      ]),
  },
  {
    title: "能不能用",
    key: "allow",
    width: 110,
    render: (row) =>
      h(
        NTag,
        { size: "small", bordered: false, type: row.allow ? "success" : "error" },
        { default: () => (row.allow ? "可以用" : "被拦下") },
      ),
  },
  {
    title: "结论来源",
    key: "layer_label",
    width: 130,
    render: (row) =>
      h(
        NTooltip,
        { trigger: "hover" },
        {
          trigger: () => h("span", { style: "font-size:12.5px" }, row.layer_label),
          default: () =>
            row.layer
              ? `命中最具体的一层：${row.layer_label}（更粗的层不再参与）`
              : "整条链都没有显式规则 → 用配置里的「指令默认策略」",
        },
      ),
  },
  {
    title: "该对象的例外",
    key: "explicit",
    width: 200,
    render: (row) => {
      if (!mxEditable.value) {
        return h("span", { style: "font-size:12px;opacity:.5" }, "等级层不支持单条指令规则");
      }
      return h(EffectSegment, {
        effect: row.explicit || "inherit",
        disabled: saving.value,
        onChange: (v: string) => setMatrixRule(row, v),
      });
    },
  },
];

const columns: DataTableColumns<CommandRow> = [
  {
    title: "指令",
    key: "name",
    minWidth: 190,
    render: (row) =>
      h("div", { style: "line-height:1.35" }, [
        h("div", { style: "font-weight:500" }, [
          row.name,
          row.is_group
            ? h(NTag, { size: "tiny", bordered: false, style: "margin-left:6px" }, { default: () => "指令组" })
            : null,
        ]),
        row.aliases?.length
          ? h("div", { style: "font-size:12px;opacity:.65" }, `别名：${row.aliases.join(" / ")}`)
          : null,
      ]),
  },
  {
    title: "说明",
    key: "desc",
    minWidth: 200,
    render: (row) => row.desc || h("span", { style: "opacity:.4" }, "-"),
  },
  {
    title: "来源",
    key: "plugin",
    width: 150,
    render: (row) => row.plugin || h("span", { style: "opacity:.4" }, "-"),
  },
  {
    title: "全局策略",
    key: "global_effect",
    width: 200,
    render: (row) =>
      h(EffectSegment, {
        effect: row.global_effect,
        disabled: saving.value,
        onChange: (v: string) => setGlobal(row, v),
      }),
  },
  {
    title: "例外",
    key: "rules",
    minWidth: 200,
    render: (row) => {
      if (!row.rule_count) return h("span", { style: "opacity:.4" }, "无");
      return h(NTooltip, { trigger: "hover" }, {
        trigger: () => h("span", { style: "font-size:12.5px" }, ruleSummary(row)),
        default: () =>
          (row.rules || [])
            .map((r) => `${scopeLabel(r)}${sceneTag(r.scene)}：${r.effect === "deny" ? "禁止" : "放行"}`)
            .join("\n"),
      });
    },
  },
  {
    title: "",
    key: "actions",
    width: 100,
    render: (row) =>
      h(NButton, { size: "tiny", quaternary: true, onClick: () => openRules(row) }, { default: () => "例外规则" }),
  },
];

onMounted(() => {
  load();
  loadMatrixObjects();
});
</script>

<template>
  <n-space vertical :size="14">
    <n-card size="small" title="指令权限说明">
      <n-space vertical :size="4" style="font-size: 13px; opacity: 0.82">
        <span>
          · 解析顺序：<b>好友专属 → 群专属 → 该指令的全局策略 → 配置里的「指令默认策略」</b>，
          <b>命中最具体的一层即止</b>。
        </span>
        <span>
          · 这一页配的是<b>单条指令</b>的规则。要「让某个人 / 某个群 / 某一等级完全不能用指令」，
          请去 <b>私聊 / 群聊</b>页的「指令权限」列，或 <b>限额 → 等级管理</b>的「默认指令权限」；
          那里设了禁止会拦下<b>所有</b>指令，且优先于本页的单条规则。
        </span>
        <span>
          · 每条指令独立一份规则：把「帮助」设为全局禁止，只影响「帮助」，不会影响别的指令。
        </span>
        <span>
          · 等级也有一层指令权限（「限额 → 等级管理 → 默认指令权限」）：等级只管
          <b>整体能不能用指令</b>，不针对某一条指令，所以它<b>压得住</b>本页单条指令的「放行」——
          要给个别人开白名单，请去「私聊 / 群聊」页把那个人的指令权限设为「放行」。
        </span>
        <span>· 管理员默认豁免（可在配置页关闭）。</span>
        <span>
          · 被拦截时会给用户发提示（文案在配置页「指令被禁用时的提示」，可用 {command} 占位），
          开关与默认策略也在配置页。
        </span>
        <span v-if="!guardEnabled" style="color: #d03050">· 当前「启用指令权限管控」是关闭的，下面的策略不会生效。</span>
        <span v-else-if="defaultEffect === 'deny'" style="color: #f0a020">
          · 当前「指令默认策略」= 禁止（白名单模式）：没有显式放行的对象用不了任何指令。
        </span>
        <span v-else>· 拦截处理器优先级：{{ priority }}（若拦截不生效，可在配置页调大这个值）。</span>
      </n-space>
    </n-card>

    <n-card size="small">
      <template #header>
        <n-space align="center" :size="10">
          <span>指令列表</span>
          <n-tag size="small" :bordered="false">{{ items.length }} 条</n-tag>
          <n-tag v-if="stale.length" size="small" type="warning" :bordered="false">
            {{ stale.length }} 条规则已失效
          </n-tag>
        </n-space>
      </template>
      <template #header-extra>
        <n-space :size="8" align="center">
          <n-input
            v-model:value="keyword"
            size="small"
            placeholder="指令名 / 说明 / 插件"
            style="width: 190px"
            clearable
          />
          <n-button size="small" @click="load">刷新</n-button>
          <n-button v-if="stale.length" size="small" :loading="pruning" @click="prune">清理失效规则</n-button>
        </n-space>
      </template>

      <n-empty
        v-if="!loading && !items.length"
        description="没有发现任何指令（指令由各插件注册，装上带指令的插件后这里会自动出现）"
        style="padding: 36px 0"
      />
      <n-data-table
        v-else
        :columns="columns"
        :data="filtered"
        :loading="loading"
        :row-key="(row: CommandRow) => row.handler || row.name"
        :bordered="false"
        :scroll-x="1000"
        size="small"
      />
    </n-card>

    <!-- 对象体检：指令 × 对象矩阵 -->
    <n-card size="small">
      <template #header>
        <n-space align="center" :size="10">
          <span>对象体检：他到底能用哪些指令</span>
          <n-tag v-if="mxSummary" size="small" :bordered="false">
            共 {{ mxSummary.total }} 条指令：可用 {{ mxSummary.allowed }} / 被拦
            {{ mxSummary.denied }}
          </n-tag>
        </n-space>
      </template>
      <n-space vertical :size="10">
        <n-space align="center" :size="8" style="flex-wrap: wrap">
          <n-select
            v-model:value="mxType"
            :options="mxTypeOptions"
            size="small"
            style="width: 110px"
            @update:value="loadMatrixObjects"
          />
          <n-select
            v-if="mxType === 'user' || mxType === 'level'"
            v-model:value="mxScene"
            :options="mxSceneOptions"
            size="small"
            style="width: 118px"
            @update:value="loadMatrix"
          />
          <n-select
            v-if="mxType === 'member'"
            v-model:value="mxGroupId"
            :options="mxGroups"
            filterable
            size="small"
            style="width: 240px"
            placeholder="先选群"
            @update:value="loadMatrixMembers"
          />
          <n-select
            v-model:value="mxId"
            :options="mxObjects"
            filterable
            clearable
            :disabled="mxType === 'global'"
            size="small"
            style="width: 260px"
            :placeholder="mxType === 'member' ? '再选成员' : '选择对象'"
            @update:value="loadMatrix"
          />
        </n-space>
        <n-spin :show="mxLoading">
          <n-empty
            v-if="!mxId"
            description="先选一个对象，马上看到它对每条指令的结论与来源"
            style="padding: 28px 0"
          />
          <template v-else>
            <n-alert
              v-if="mxType === 'level'"
              type="info"
              :bordered="false"
              style="margin-bottom: 8px"
            >
              这是「{{ mxLevelKind === "group" ? "群聊" : "好友" }}等级」的结论：等级只管
              <b>整体能不能用指令</b>（在「限额 → 等级管理」配置），单条指令的例外要配到
              好友 / 群 / 群成员上。想给这一档里的个别人开白名单，去「私聊 / 群聊」页设那个人的指令权限。
            </n-alert>
            <n-data-table
              :columns="mxColumns"
              :data="mxRows"
              :bordered="false"
              size="small"
              :max-height="460"
              :scroll-x="620"
              :row-key="(row: CommandMatrixRow) => row.name"
            />
          </template>
        </n-spin>
      </n-space>
    </n-card>

    <!-- 例外规则 -->
    <n-modal
      v-model:show="showRules"
      preset="card"
      :title="`例外规则：${current?.name || ''}`"
      style="width: 620px"
    >
      <n-space vertical :size="12">
        <n-space align="center" :size="8">
          <n-radio-group v-model:value="newRule.scope_type" size="small">
            <n-radio-button value="user">好友</n-radio-button>
            <n-radio-button value="group">群</n-radio-button>
          </n-radio-group>
          <n-input
            v-model:value="newRule.scope_id"
            size="small"
            :placeholder="newRule.scope_type === 'user' ? 'QQ 号' : '群号'"
            style="width: 150px"
          />
          <n-select
            v-model:value="newRule.effect"
            size="small"
            style="width: 110px"
            :options="[
              { label: '禁止', value: 'deny' },
              { label: '放行', value: 'allow' },
            ]"
          />
          <n-button size="small" type="primary" :loading="adding" @click="addRule">添加</n-button>
        </n-space>
        <span style="font-size: 12px; opacity: 0.62">
          「放行」用于在「默认禁止 / 全局禁止」的前提下给个别对象开白名单。
        </span>

        <n-empty v-if="!current?.rules?.length" description="还没有例外规则" style="padding: 16px 0" />
        <n-data-table
          v-else
          :columns="[
            {
              title: '类型',
              key: 'scope_type',
              width: 90,
              render: (r: any) => (r.scope_type === 'user' ? '好友' : '群'),
            },
            { title: '对象', key: 'scope_id' },
            {
              title: '效果',
              key: 'effect',
              width: 100,
              render: (r: any) =>
                h(
                  NTag,
                  { size: 'small', type: r.effect === 'deny' ? 'error' : 'success', bordered: false },
                  { default: () => (r.effect === 'deny' ? '禁止' : '放行') },
                ),
            },
            {
              title: '操作',
              key: 'actions',
              width: 90,
              render: (r: any) =>
                h(
                  NButton,
                  {
                    size: 'tiny',
                    quaternary: true,
                    onClick: () => current && removeRule(current, r.scope_type, r.scope_id),
                  },
                  { default: () => '删除' },
                ),
            },
          ]"
          :data="current?.rules || []"
          :bordered="false"
          size="small"
          :max-height="280"
        />
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showRules = false">关闭</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-space>
</template>
