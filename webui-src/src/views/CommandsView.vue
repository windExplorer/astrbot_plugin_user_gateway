<script setup lang="ts">
// 指令：按指令名管控「全局 / 好友 / 群」的可用性（默认放行，可改成白名单模式）。
//
// 拦截机制（后端）：本插件注册了一个「所有消息 + 高优先级」的处理器，
// AstrBot 的 process 阶段按优先级依次执行 handler 且逐个检查 is_stopped()，
// 所以在这里 stop_event() 就能让后面的指令 handler 不执行。
import { computed, h, onMounted, ref } from "vue";
import {
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
  NTag,
  NTooltip,
  useMessage,
  type DataTableColumns,
} from "naive-ui";

import { apiCommands, apiPruneCommands, apiSetPolicy, type CommandRow } from "../api";
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

function ruleSummary(row: CommandRow): string {
  const rules = row.rules || [];
  if (!rules.length) return "";
  const text = rules
    .slice(0, 2)
    .map((r) => `${r.scope_type === "user" ? "好友" : "群"} ${r.scope_id} ${r.effect === "deny" ? "禁止" : "放行"}`)
    .join("；");
  return rules.length > 2 ? `${text} 等 ${rules.length} 条` : text;
}

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
            .map((r) => `${r.scope_type === "user" ? "好友" : "群"} ${r.scope_id}：${r.effect === "deny" ? "禁止" : "放行"}`)
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

onMounted(load);
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
