<script setup lang="ts">
// 配置：分区表单 + 后端信息。分区表**显式列出**每个顶层键——_conf_schema.json 新增键时
// 必须同步加进这里（或依赖兜底的「其他」分区），否则用户会看不到新配置项。
import { computed, onMounted, ref } from "vue";
import {
  NAlert,
  NButton,
  NCard,
  NDescriptions,
  NDescriptionsItem,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NModal,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpace,
  NSwitch,
  NTag,
  useMessage,
} from "naive-ui";

import {
  apiExportRules,
  apiGet,
  apiImportRules,
  apiPost,
  type ConfigPayload,
  type ImportStats,
  type PingInfo,
} from "../api";

const message = useMessage();
const loading = ref(false);
const saving = ref(false);
const items = ref<Record<string, any>>({});
const schema = ref<Record<string, any>>({});
const ping = ref<PingInfo | null>(null);

// ------ 备份与迁移（规则导入导出） ------
const exporting = ref(false);
const importing = ref(false);
const importMode = ref<"merge" | "replace">("merge");
const importText = ref("");
const importStats = ref<ImportStats | null>(null);
const exportOpen = ref(false);
const exportName = ref("");
const exportText = ref("");
const exportMeta = ref<string>("");

async function doExport() {
  exporting.value = true;
  try {
    const res = await apiExportRules();
    exportName.value = res.filename;
    exportText.value = res.content;
    const c = res.meta?.counts || {};
    exportMeta.value =
      `权限 ${c.policies ?? 0} 条 · 等级 ${c.levels ?? 0} 个 · 归级 ${c.subject_levels ?? 0} 条 · 额度 ${c.quotas ?? 0} 条` +
      `（导出时间 ${new Date().toLocaleString()}）`;
    const blob = new Blob([res.content], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = res.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    message.success(`已导出 ${res.filename}（若浏览器拦截下载，可在弹窗里复制）`);
    exportOpen.value = true;
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    exporting.value = false;
  }
}

async function copyExport() {
  const text = exportText.value || "";
  try {
    await navigator.clipboard.writeText(text);
    message.success("已复制到剪贴板");
    return;
  } catch {
    /* 沙箱里可能没有剪贴板权限，退回 execCommand */
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  ta.remove();
  if (ok) message.success("已复制到剪贴板");
  else message.warning("复制失败，请手动全选文本复制");
}

function clearImport() {
  importText.value = "";
  importStats.value = null;
}

/** 读本地文件（沙箱 iframe 里 FileReader 可用，file input 也能用）。 */
function pickFile(ev: Event) {
  const input = ev.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    importText.value = String(reader.result || "");
    message.success(`已读入 ${file.name}（${importText.value.length} 字符）`);
  };
  reader.onerror = () => message.error("读取文件失败");
  reader.readAsText(file);
  input.value = ""; // 同一个文件能连续选
}

async function doImport() {
  if (!importText.value.trim()) {
    message.warning("请粘贴导出内容，或选择一个 .json 文件");
    return;
  }
  if (importMode.value === "replace") {
    const ok = window.confirm(
      "「覆盖」会先清空当前的权限规则、等级、归级与额度，再导入文件内容。\n" +
        "用量统计、明细与审计不受影响。确定继续？",
    );
    if (!ok) return;
  }
  importing.value = true;
  try {
    const stats = await apiImportRules(importMode.value, importText.value);
    importStats.value = stats;
    message.success(
      `导入完成：等级 ${stats.levels} / 归级 ${stats.subject_levels} / 额度 ${stats.quotas} / 权限 ${stats.policies}` +
        (stats.skipped ? `，跳过 ${stats.skipped} 条不合法数据` : ""),
    );
  } catch (e: any) {
    message.error(e?.message || String(e), { duration: 8000 });
  } finally {
    importing.value = false;
  }
}

// 分区定义（顺序即页面顺序；未列出的键会落进「其他」）
const GROUPS: { name: string; hint: string; keys: string[] }[] = [
  { name: "总开关", hint: "整个插件与各功能模块的开关", keys: ["enabled", "llm_guard_enabled", "quota_enabled", "stats_enabled"] },
  { name: "策略", hint: "默认放行还是默认禁止、管理员是否豁免", keys: ["default_effect", "admin_exempt"] },
  { name: "指令权限", hint: "指令的黑白名单（具体规则在「指令」页配置）", keys: ["command_guard_enabled", "default_command_effect", "command_deny_notice", "command_guard_priority", "track_command_usage"] },
  { name: "拒绝行为", hint: "被拦截时如何提示用户与管理员", keys: ["deny_notice", "silent", "notice_cooldown_sec", "notify_admin"] },
  { name: "额度", hint: "token 限额的计数口径与工作模式", keys: ["quota_mode", "count_cached_tokens", "warn_ratio"] },
  { name: "同步", hint: "好友 / 群列表的同步与头像", keys: ["sync_interval_min", "sync_timeout_sec", "sync_group_members", "load_avatars", "avatar_source", "avatar_cache_days", "avatar_timeout_sec"] },
  { name: "最后回复", hint: "记录 bot 在每个好友 / 群里的最后一条回复", keys: ["track_bot_messages"] },
  { name: "模型路由", hint: "按等级给会话指定主 / 备用模型；用户也能用「/切换模型」在等级圈定的名单里自助换", keys: ["model_route_enabled", "model_route_failure_threshold", "model_route_circuit_sec", "model_switch_enabled", "model_switch_timeout_sec", "model_card_font"] },
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

    <n-card size="small" title="备份与迁移">
      <template #header-extra>
        <span style="font-size: 12px; opacity: 0.6">导出 / 导入规则（权限 · 等级 · 归级 · 额度）</span>
      </template>
      <n-space vertical :size="12">
        <n-space align="center" :size="10" style="flex-wrap: wrap">
          <n-button size="small" type="primary" ghost :loading="exporting" @click="doExport">导出规则</n-button>
          <span style="font-size: 12px; opacity: 0.65">
            不含用量统计、明细与审计（那些是历史数据）。要连历史一起备份，请直接备份数据库文件。
          </span>
        </n-space>

        <n-space align="center" :size="8">
          <span style="font-size: 13px">导入方式</span>
          <n-radio-group v-model:value="importMode" size="small">
            <n-radio-button value="merge">合并（只覆盖文件里出现的条目）</n-radio-button>
            <n-radio-button value="replace">覆盖（先清空再导入）</n-radio-button>
          </n-radio-group>
        </n-space>

        <n-input
          v-model:value="importText"
          type="textarea"
          :rows="6"
          placeholder="把导出的 JSON 粘贴到这里，或点右边的「选择文件…」"
        />

        <n-space align="center" :size="8" style="flex-wrap: wrap">
          <n-button size="small" type="primary" :loading="importing" @click="doImport">开始导入</n-button>
          <label class="file-pick">
            选择文件…
            <input type="file" accept=".json,application/json" @change="pickFile" />
          </label>
          <n-button size="small" quaternary @click="clearImport">清空</n-button>
          <span style="font-size: 12px; opacity: 0.6">
            导入会按「等级名」重建等级并自动修正归级与等级额度的引用；不合法的行会被跳过。
          </span>
        </n-space>

        <n-alert
          v-if="importStats"
          :type="importStats.skipped ? 'warning' : 'success'"
          :bordered="false"
          size="small"
        >
          导入完成（{{ importStats.mode === "replace" ? "覆盖" : "合并" }}）：
          等级 {{ importStats.levels }} · 归级 {{ importStats.subject_levels }} ·
          额度 {{ importStats.quotas }} · 权限 {{ importStats.policies }}
          <template v-if="importStats.skipped">，跳过 {{ importStats.skipped }} 条不合法数据</template>
        </n-alert>
      </n-space>
    </n-card>

    <n-modal v-model:show="exportOpen" preset="card" :title="`导出内容：${exportName}`" style="width: 780px; max-width: 94vw">
      <n-space vertical :size="10">
        <span style="font-size: 12px; opacity: 0.7">{{ exportMeta }}</span>
        <span style="font-size: 12px; opacity: 0.65">
          若浏览器拦截了自动下载，点「复制」把内容存成 .json 文件即可（导入时粘贴回来）。
        </span>
        <n-input v-model:value="exportText" type="textarea" :rows="16" readonly />
        <n-space justify="end">
          <n-button size="small" @click="exportOpen = false">关闭</n-button>
          <n-button size="small" type="primary" @click="copyExport">复制</n-button>
        </n-space>
      </n-space>
    </n-modal>
  </n-space>
</template>

<style scoped>
/* 用 label 包一个隐藏的 file input：沙箱 iframe 里 n-upload 走网络上传没必要，
   本地读文件用 FileReader 更直接。 */
.file-pick {
  display: inline-flex;
  align-items: center;
  height: 28px;
  padding: 0 12px;
  font-size: 13px;
  border: 1px solid var(--n-border-color, #e0e0e6);
  border-radius: 6px;
  cursor: pointer;
  user-select: none;
}
.file-pick:hover {
  border-color: #7c5cff;
  color: #7c5cff;
}
.file-pick input {
  display: none;
}
</style>
