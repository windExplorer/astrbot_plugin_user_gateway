<script setup lang="ts">
// 配置：分区表单 + 后端信息。分区表**显式列出**每个顶层键——_conf_schema.json 新增键时
// 必须同步加进这里（或依赖兜底的「其他」分区），否则用户会看不到新配置项。
import { computed, onMounted, ref } from "vue";
import {
  NButton,
  NCard,
  NDescriptions,
  NDescriptionsItem,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NSelect,
  NSpace,
  NSwitch,
  NTag,
  useMessage,
} from "naive-ui";

import { apiGet, apiPost, type ConfigPayload, type PingInfo } from "../api";

const message = useMessage();
const loading = ref(false);
const saving = ref(false);
const items = ref<Record<string, any>>({});
const schema = ref<Record<string, any>>({});
const ping = ref<PingInfo | null>(null);

// 分区定义（顺序即页面顺序；未列出的键会落进「其他」）
const GROUPS: { name: string; hint: string; keys: string[] }[] = [
  { name: "总开关", hint: "整个插件与各功能模块的开关", keys: ["enabled", "llm_guard_enabled", "quota_enabled", "stats_enabled"] },
  { name: "策略", hint: "默认放行还是默认禁止、管理员是否豁免", keys: ["default_effect", "admin_exempt"] },
  { name: "拒绝行为", hint: "被拦截时如何提示用户与管理员", keys: ["deny_notice", "silent", "notice_cooldown_sec", "notify_admin"] },
  { name: "额度", hint: "token 限额的计数口径与工作模式", keys: ["quota_mode", "count_cached_tokens", "warn_ratio"] },
  { name: "同步", hint: "好友 / 群列表的同步与头像", keys: ["sync_interval_min", "sync_timeout_sec", "load_avatars", "avatar_source", "avatar_cache_days", "avatar_timeout_sec"] },
  { name: "最后回复", hint: "记录 bot 在每个好友 / 群里的最后一条回复", keys: ["track_bot_messages"] },
  { name: "统计", hint: "用量明细的保留策略", keys: ["retention_days"] },
  { name: "高级", hint: "排查问题时才需要改", keys: ["guard_priority", "debug_log"] },
];

const schemaKeys = computed(() => Object.keys(schema.value || {}));

const otherKeys = computed(() => {
  const listed = new Set(GROUPS.flatMap((g) => g.keys));
  return schemaKeys.value.filter((k) => !listed.has(k));
});

function specOf(key: string) {
  return schema.value?.[key] || {};
}

function hintOf(key: string): string {
  const s = specOf(key);
  return String(s.hint || s.description || "");
}

function labelOf(key: string): string {
  const s = specOf(key);
  return String(s.description || key);
}

async function load() {
  loading.value = true;
  try {
    const res = await apiGet<ConfigPayload>("/config");
    items.value = res.items || {};
    schema.value = res.schema || {};
    ping.value = await apiGet<PingInfo>("/ping").catch(() => null);
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const res = await apiPost<{ changed: string[] }>("/config", { items: items.value });
    message.success(`已保存 ${res.changed?.length || 0} 项配置`);
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <n-space vertical :size="14">
    <n-card size="small" title="运行信息">
      <n-descriptions :column="2" label-placement="left" size="small" bordered>
        <n-descriptions-item label="插件版本">
          <n-tag size="small" type="info" :bordered="false">v{{ ping?.version || "-" }}</n-tag>
        </n-descriptions-item>
        <n-descriptions-item label="数据库状态">
          <n-tag size="small" :type="ping?.db_ready ? 'success' : 'error'" :bordered="false">
            {{ ping?.db_ready ? "已就绪" : "未就绪" }}
          </n-tag>
        </n-descriptions-item>
        <n-descriptions-item label="数据目录">{{ ping?.data_dir || "-" }}</n-descriptions-item>
        <n-descriptions-item label="数据库文件">{{ ping?.db_path || "-" }}</n-descriptions-item>
      </n-descriptions>
    </n-card>

    <n-card v-for="g in GROUPS" :key="g.name" size="small" :title="g.name">
      <template #header-extra>
        <span style="font-size: 12px; opacity: 0.6">{{ g.hint }}</span>
      </template>
      <n-form label-placement="top" :show-feedback="true">
        <n-form-item v-for="key in g.keys" :key="key" :label="labelOf(key)">
          <!-- 布尔 -->
          <n-switch v-if="specOf(key).type === 'bool'" v-model:value="items[key]" />
          <!-- 数值 -->
          <n-input-number
            v-else-if="specOf(key).type === 'int' || specOf(key).type === 'float'"
            v-model:value="items[key]"
            :step="specOf(key).type === 'float' ? 0.1 : 1"
            style="width: 220px"
          />
          <!-- 枚举 -->
          <n-select
            v-else-if="specOf(key).options"
            v-model:value="items[key]"
            :options="specOf(key).options.map((o: string) => ({ label: o, value: o }))"
            style="width: 220px"
          />
          <!-- 长文本 -->
          <n-input
            v-else-if="specOf(key).type === 'text'"
            v-model:value="items[key]"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 5 }"
          />
          <!-- 普通字符串 -->
          <n-input v-else v-model:value="items[key]" style="max-width: 520px" />
          <template #feedback>
            <span style="font-size: 12px; opacity: 0.62; line-height: 1.5">{{ hintOf(key) }}</span>
          </template>
        </n-form-item>
      </n-form>
    </n-card>

    <n-card v-if="otherKeys.length" size="small" title="其他">
      <template #header-extra>
        <span style="font-size: 12px; opacity: 0.6">未归入任何分区的配置键（请把它们加进 ConfigView 的分区表）</span>
      </template>
      <n-form label-placement="top">
        <n-form-item v-for="key in otherKeys" :key="key" :label="labelOf(key)">
          <n-switch v-if="specOf(key).type === 'bool'" v-model:value="items[key]" />
          <n-input-number
            v-else-if="specOf(key).type === 'int' || specOf(key).type === 'float'"
            v-model:value="items[key]"
            style="width: 220px"
          />
          <n-input v-else v-model:value="items[key]" style="max-width: 520px" />
          <template #feedback>
            <span style="font-size: 12px; opacity: 0.62">{{ hintOf(key) }}</span>
          </template>
        </n-form-item>
      </n-form>
    </n-card>

    <n-space justify="end">
      <n-button size="small" @click="load">重新载入</n-button>
      <n-button size="small" type="primary" :loading="saving" @click="save">保存配置</n-button>
    </n-space>
  </n-space>
</template>
