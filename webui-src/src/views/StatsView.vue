<script setup lang="ts">
// 统计：用量明细（筛选 / 分页 / 导出 CSV）+ 指令维度（Top 榜 + 时段热力图）+ 操作审计。
//
// 数据来源都是 usage_log 这一张表，`kind` 区分维度：
//   llm     → LLM 调用与被拒（token 统计口径）
//   command → 指令触发与被拦（指令统计口径）
// 两者互不干扰：把 LLM 的默认策略设成禁止，也不会让指令的统计变脏。
import { computed, h, onMounted, ref } from "vue";
import {
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NGrid,
  NGridItem,
  NInput,
  NModal,
  NPagination,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpace,
  NSpin,
  NTabPane,
  NTabs,
  NTag,
  NTooltip,
  useMessage,
  type DataTableColumns,
} from "naive-ui";

import {
  apiAudit,
  apiCommandStats,
  apiExportUsage,
  apiUsage,
  type AuditRow,
  type CommandStats,
  type UsageFilter,
  type UsageRow,
} from "../api";
import EChart from "../EChart.vue";

const message = useMessage();
const loading = ref(false);
const exporting = ref(false);

const range = ref<"1d" | "7d" | "30d">("7d");
const kind = ref<string>("");
const status = ref<string>("");
const scopeId = ref("");
const senderId = ref("");

const rows = ref<UsageRow[]>([]);
const total = ref(0);
const page = ref(1);
const size = ref(50);

const stats = ref<CommandStats | null>(null);
const audits = ref<AuditRow[]>([]);
const auditTotal = ref(0);
const auditPage = ref(1);
const auditSize = ref(20);

// Tab 分区：指令维度 / 用量明细 / 操作审计。审计**懒加载**（第一次切过去才拉），
// 初始只拉指令统计 + 明细，省一次查询也快一截。
const tab = ref("commands");
const auditLoaded = ref(false);

async function onTabChange(name: string) {
  if (name === "audit" && !auditLoaded.value) {
    auditLoaded.value = true;
    await loadAudit();
  }
}

const csvOpen = ref(false);
const csvName = ref("");
const csvText = ref("");

const kindOptions = [
  { label: "全部类型", value: "" },
  { label: "LLM 调用", value: "llm" },
  { label: "指令", value: "command" },
];
const statusOptions = [
  { label: "全部状态", value: "" },
  { label: "正常", value: "ok" },
  { label: "被拒绝", value: "denied" },
];
const sizeOptions = [
  { label: "每页 20", value: 20 },
  { label: "每页 50", value: 50 },
  { label: "每页 100", value: 100 },
  { label: "每页 200", value: 200 },
];

/** 当前筛选条件（明细与导出共用同一份，保证「看到的」=「导出的」）。 */
function filter(): UsageFilter {
  return {
    range: range.value,
    kind: kind.value || undefined,
    status: status.value || undefined,
    scope_id: scopeId.value.trim() || undefined,
    sender_id: senderId.value.trim() || undefined,
    page: page.value,
    size: size.value,
  };
}

function fmtTime(ts: number): string {
  return ts ? new Date(ts * 1000).toLocaleString() : "-";
}

function fmtNum(n: number | undefined | null): string {
  const v = Number(n || 0);
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(2) + "M";
  if (v >= 1_000) return (v / 1_000).toFixed(1) + "K";
  return String(v);
}

function totalTok(r: UsageRow): number {
  return Number(r.tok_in_other || 0) + Number(r.tok_in_cached || 0) + Number(r.tok_out || 0);
}

const isCommand = (r: UsageRow) => String(r.kind) === "command";

const statusMeta = (r: UsageRow) => {
  if (String(r.status) === "denied") return { type: "error" as const, text: "被拒绝" };
  if (String(r.status) === "error") return { type: "warning" as const, text: "失败" };
  return { type: "success" as const, text: "正常" };
};

async function loadUsage() {
  try {
    const res = await apiUsage(filter());
    rows.value = res.rows || [];
    total.value = Number(res.total || 0);
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function loadStats() {
  try {
    stats.value = await apiCommandStats(range.value, 10);
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function loadAudit() {
  try {
    const res = await apiAudit(auditPage.value, auditSize.value);
    audits.value = res.rows || [];
    auditTotal.value = Number(res.total || 0);
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function loadAll() {
  loading.value = true;
  try {
    const jobs: Promise<void>[] = [loadUsage(), loadStats()];
    if (auditLoaded.value) jobs.push(loadAudit());
    await Promise.all(jobs);
  } finally {
    loading.value = false;
  }
}

function search() {
  page.value = 1;
  loadUsage();
}

function resetFilters() {
  kind.value = "";
  status.value = "";
  scopeId.value = "";
  senderId.value = "";
  page.value = 1;
  loadUsage();
}

function setRange(v: "1d" | "7d" | "30d") {
  range.value = v;
  page.value = 1;
  loadAll();
}

/** 导出：后端生成 CSV 文本，这里用 Blob 落地。
 *
 *  页面跑在 sandbox iframe 里，下载可能被浏览器拦（没给 allow-downloads）——所以
 *  同时把内容放进弹窗，用户可以直接复制粘贴，不依赖下载权限。 */
async function exportCsv() {
  exporting.value = true;
  try {
    const res = await apiExportUsage(filter());
    csvName.value = res.filename;
    csvText.value = res.content;
    const blob = new Blob([res.content], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = res.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    message.success(`已导出 ${res.filename}（若浏览器拦截了下载，可在弹窗里复制）`);
    csvOpen.value = true;
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    exporting.value = false;
  }
}

async function copyCsv() {
  const text = csvText.value || "";
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

onMounted(loadAll);

// ------ 明细表 ------
const usageColumns: DataTableColumns<UsageRow> = [
  {
    title: "时间",
    key: "ts",
    width: 150,
    render: (r) =>
      h(NTooltip, { trigger: "hover" }, {
        trigger: () => h("span", { style: "font-size:12.5px" }, fmtTime(r.ts)),
        default: () => fmtTime(r.ts),
      }),
  },
  {
    title: "类型",
    key: "kind",
    width: 80,
    render: (r) =>
      h(
        NTag,
        { size: "small", bordered: false, type: isCommand(r) ? "info" : "default" },
        { default: () => (isCommand(r) ? "指令" : "LLM") },
      ),
  },
  {
    title: "对象",
    key: "scope_id",
    width: 150,
    render: (r) =>
      h("div", { style: "line-height:1.35" }, [
        h("div", { style: "font-size:12.5px" }, `${String(r.scope_type) === "group" ? "群" : "好友"} ${r.scope_id || "-"}`),
        h("div", { style: "font-size:11.5px;opacity:.6" }, `发起人 ${r.sender_id || "-"}`),
      ]),
  },
  {
    title: "指令 / 模型",
    key: "command_name",
    minWidth: 170,
    render: (r) => {
      if (isCommand(r)) {
        return r.command_name
          ? h("span", { style: "font-size:12.5px" }, r.command_name)
          : h("span", { style: "opacity:.4" }, "-");
      }
      return h("div", { style: "font-size:12.5px;line-height:1.35" }, [
        h("div", {}, r.model || "(未知模型)"),
        h("div", { style: "font-size:11.5px;opacity:.6" }, r.provider_id || ""),
      ]);
    },
  },
  {
    title: "状态",
    key: "status",
    width: 110,
    render: (r) => {
      const meta = statusMeta(r);
      if (!r.deny_reason) {
        return h(NTag, { size: "small", bordered: false, type: meta.type }, { default: () => meta.text });
      }
      return h(NTooltip, { trigger: "hover" }, {
        trigger: () => h(NTag, { size: "small", bordered: false, type: meta.type }, { default: () => meta.text }),
        default: () => `原因：${r.deny_reason}`,
      });
    },
  },
  {
    title: "token（输入 / 缓存 / 输出）",
    key: "tok",
    width: 200,
    render: (r) => {
      if (isCommand(r)) return h("span", { style: "opacity:.4" }, "-");
      return h(NTooltip, { trigger: "hover" }, {
        trigger: () =>
          h("span", { style: "font-size:12.5px" }, `${fmtNum(r.tok_in_other)} / ${fmtNum(r.tok_in_cached)} / ${fmtNum(r.tok_out)}`),
        default: () =>
          `合计 ${totalTok(r)} token${Number(r.estimated) ? "（含估算）" : ""}`,
      });
    },
  },
  {
    title: "合计",
    key: "total",
    width: 95,
    render: (r) => {
      if (isCommand(r)) return h("span", { style: "opacity:.4" }, "-");
      return h("span", {}, [
        fmtNum(totalTok(r)),
        Number(r.estimated) ? h(NTag, { size: "tiny", bordered: false, type: "warning", style: "margin-left:4px" }, { default: () => "估" }) : null,
      ]);
    },
  },
  {
    title: "延迟",
    key: "latency_ms",
    width: 85,
    render: (r) => (r.latency_ms ? `${r.latency_ms} ms` : h("span", { style: "opacity:.4" }, "-")),
  },
];

const auditColumns: DataTableColumns<AuditRow> = [
  { title: "时间", key: "ts", width: 160, render: (r) => fmtTime(r.ts) },
  { title: "操作者", key: "actor", width: 90, render: (r) => r.actor || "-" },
  {
    title: "操作",
    key: "action",
    width: 150,
    render: (r) => h(NTag, { size: "small", bordered: false }, { default: () => r.action || "-" }),
  },
  {
    title: "内容",
    key: "payload",
    render: (r) =>
      h(NTooltip, { trigger: "hover", placement: "top" }, {
        trigger: () => h("span", { style: "font-size:12.5px;word-break:break-all" }, (r.payload || "").slice(0, 90)),
        default: () => h("div", { style: "max-width:520px;white-space:pre-wrap;word-break:break-all;font-size:12px" }, r.payload || "-"),
      }),
  },
];

// ------ 图表 ------
function barOption(list: { command: string; cnt: number }[], color: string) {
  const data = list.slice().reverse();
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 6, right: 42, top: 8, bottom: 6, containLabel: true },
    xAxis: { type: "value" },
    yAxis: {
      type: "category",
      data: data.map((r) => r.command),
      axisLabel: { width: 120, overflow: "truncate", fontSize: 11.5 },
    },
    series: [
      {
        type: "bar",
        barMaxWidth: 15,
        itemStyle: { borderRadius: [0, 4, 4, 0], color },
        label: { show: true, position: "right", fontSize: 11 },
        data: data.map((r) => r.cnt),
      },
    ],
  };
}

const okBar = computed(() => barOption(stats.value?.top_ok || [], "#18a058"));
const deniedBar = computed(() => barOption(stats.value?.top_denied || [], "#d03050"));

const DOW = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

const heatOption = computed(() => {
  const hours = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, "0"));
  const map = new Map<string, number>();
  (stats.value?.heatmap || []).forEach((r) => map.set(`${r.dow}-${r.hour}`, Number(r.cnt || 0)));
  const data: [number, number, number][] = [];
  let max = 0;
  for (let h = 0; h < 24; h += 1) {
    for (let d = 0; d < 7; d += 1) {
      const v = map.get(`${d}-${h}`) || 0;
      if (v > max) max = v;
      data.push([h, d, v]);
    }
  }
  return {
    tooltip: {
      position: "top",
      formatter: (p: any) => `${DOW[p.value[1]]} ${hours[p.value[0]]}:00 · ${p.value[2]} 次`,
    },
    grid: { left: 54, right: 20, top: 10, bottom: 62 },
    xAxis: { type: "category", data: hours, splitArea: { show: true }, axisLabel: { fontSize: 10.5 } },
    yAxis: { type: "category", data: DOW, splitArea: { show: true }, axisLabel: { fontSize: 11 } },
    visualMap: {
      min: 0,
      max: Math.max(max, 1),
      calculable: true,
      orient: "horizontal",
      left: "center",
      bottom: 0,
      itemWidth: 12,
      itemHeight: 90,
      textStyle: { fontSize: 11 },
      inRange: { color: ["#eef1fb", "#a493ff", "#5b3fd6", "#2c1a78"] },
    },
    series: [
      {
        type: "heatmap",
        data,
        progressive: 2000,
        emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,.3)" } },
      },
    ],
  };
});
</script>

<template>
  <n-space vertical :size="14">
    <n-card size="small" title="统计说明">
      <n-space vertical :size="4" style="font-size: 13px; opacity: 0.82">
        <span>
          · <b>LLM 调用</b> 与 <b>指令</b> 是两套独立口径：token 统计只算 LLM，指令只记「谁在什么时候用了/被拦了什么指令」。
        </span>
        <span>
          · 「被拒绝」包含 LLM 被权限或额度拦下、以及指令被指令权限拦下，具体原因把鼠标移到状态上可看。
        </span>
        <span v-if="stats && !stats.track_enabled" style="color: #f0a020">
          · 当前「记录指令触发流水」是关闭的，所以「最常触发的指令」为空（被拦的仍会记录）。
          可在「配置 → 指令权限」打开。
        </span>
      </n-space>
    </n-card>

    <n-space align="center" justify="space-between" :size="10" style="flex-wrap: wrap">
      <n-space align="center" :size="8" style="flex-wrap: wrap">
        <n-radio-group :value="range" size="small" @update:value="setRange">
          <n-radio-button value="1d">今日</n-radio-button>
          <n-radio-button value="7d">近 7 日</n-radio-button>
          <n-radio-button value="30d">近 30 日</n-radio-button>
        </n-radio-group>
        <n-select v-model:value="kind" size="small" style="width: 118px" :options="kindOptions" @update:value="search" />
        <n-select v-model:value="status" size="small" style="width: 118px" :options="statusOptions" @update:value="search" />
        <n-input v-model:value="scopeId" size="small" style="width: 130px" placeholder="对象 ID" clearable @keyup.enter="search" />
        <n-input v-model:value="senderId" size="small" style="width: 130px" placeholder="发起人 QQ" clearable @keyup.enter="search" />
        <n-button size="small" type="primary" ghost @click="search">查询</n-button>
        <n-button size="small" quaternary @click="resetFilters">重置</n-button>
      </n-space>
      <n-space align="center" :size="8">
        <n-button size="small" :loading="exporting" @click="exportCsv">导出 CSV</n-button>
        <n-button size="small" @click="loadAll">刷新</n-button>
      </n-space>
    </n-space>

    <n-spin :show="loading">
      <!-- 三个 Tab 分区，避免「指令图表 → 热力图 → 明细 → 审计」一路滚到底 -->
      <n-tabs v-model:value="tab" type="line" animated @update:value="onTabChange">
        <n-tab-pane name="commands" tab="指令维度">
          <n-grid :cols="2" :x-gap="12" :y-gap="12" item-responsive responsive="screen">
            <n-grid-item span="2 m:1">
              <n-card size="small" title="最常触发的指令" :bordered="false" embedded>
                <n-empty v-if="!stats?.top_ok?.length" description="暂无记录（可能没开「记录指令触发流水」）" />
                <EChart v-else :option="okBar" height="300px" />
              </n-card>
            </n-grid-item>
            <n-grid-item span="2 m:1">
              <n-card size="small" title="最常被拦的指令" :bordered="false" embedded>
                <n-empty v-if="!stats?.top_denied?.length" description="暂无拦截记录" />
                <EChart v-else :option="deniedBar" height="300px" />
              </n-card>
            </n-grid-item>
          </n-grid>
          <n-card size="small" title="LLM 活跃时段（24 小时 × 星期）" style="margin-top: 12px" :bordered="false" embedded>
            <n-empty v-if="!stats?.heatmap?.length" description="暂无数据" />
            <EChart v-else :option="heatOption" height="300px" />
          </n-card>
        </n-tab-pane>

        <n-tab-pane name="usage" tab="用量明细">
          <n-card size="small" :bordered="false" embedded>
            <template #header>
              <n-space align="center" :size="8">
                <span>明细</span>
                <n-tag size="small" :bordered="false">共 {{ total }} 条</n-tag>
              </n-space>
            </template>
            <template #header-extra>
              <n-select v-model:value="size" size="small" style="width: 108px" :options="sizeOptions" @update:value="search" />
            </template>
            <n-data-table
              :columns="usageColumns"
              :data="rows"
              :bordered="false"
              size="small"
              :max-height="460"
              :row-key="(r: UsageRow) => r.id"
            />
            <n-space justify="center" style="margin-top: 12px">
              <n-pagination
                v-model:page="page"
                :page-count="Math.max(1, Math.ceil(total / size))"
                :page-size="size"
                @update:page="loadUsage"
              >
                <template #prefix="{ itemCount }">共 {{ itemCount }} 条</template>
              </n-pagination>
            </n-space>
          </n-card>
        </n-tab-pane>

        <n-tab-pane name="audit" tab="操作审计">
          <n-card size="small" :bordered="false" embedded>
            <template #header>
              <n-space align="center" :size="8">
                <span>审计</span>
                <n-tag size="small" :bordered="false">共 {{ auditTotal }} 条</n-tag>
              </n-space>
            </template>
            <n-data-table
              :columns="auditColumns"
              :data="audits"
              :bordered="false"
              size="small"
              :max-height="460"
              :row-key="(r: AuditRow) => r.id"
            />
            <n-space justify="center" style="margin-top: 12px">
              <n-pagination
                v-model:page="auditPage"
                :page-count="Math.max(1, Math.ceil(auditTotal / auditSize))"
                :page-size="auditSize"
                @update:page="loadAudit"
              />
            </n-space>
          </n-card>
        </n-tab-pane>
      </n-tabs>
    </n-spin>

    <n-modal v-model:show="csvOpen" preset="card" :title="`导出内容：${csvName}`" style="width: 720px">
      <n-space vertical :size="10">
        <span style="font-size: 12px; opacity: 0.65">
          若浏览器拦截了自动下载，点「复制」把内容粘到记事本，另存为 .csv 即可（已带 UTF-8 BOM，Excel 不乱码）。
        </span>
        <n-input v-model:value="csvText" type="textarea" :rows="16" readonly />
        <n-space justify="end">
          <n-button size="small" @click="csvOpen = false">关闭</n-button>
          <n-button size="small" type="primary" @click="copyCsv">复制</n-button>
        </n-space>
      </n-space>
    </n-modal>
  </n-space>
</template>
