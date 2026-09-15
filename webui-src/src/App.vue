<script setup lang="ts">
import { computed, h, onMounted, ref } from "vue";
import { RouterLink, useRoute } from "vue-router";
import {
  NConfigProvider,
  NLayout,
  NLayoutContent,
  NLayoutHeader,
  NLayoutSider,
  NMenu,
  NTag,
  NButton,
  NSpace,
  NTooltip,
  darkTheme,
  zhCN,
  dateZhCN,
  NMessageProvider,
  type GlobalThemeOverrides,
} from "naive-ui";

import { apiGet, type PingInfo } from "./api";
import { getContext, onContext, storageGet, storageSet } from "./bridge";
import { PLUGIN_VERSION } from "./version";

const route = useRoute();

// 主题：优先用户本次的手动选择，否则跟随 AstrBot 面板下发的 context.isDark。
// ⚠️ 页面运行在 sandbox iframe 里，localStorage 不可直接访问（会抛 SecurityError），
//    storageGet/storageSet 已做兜底（不可用时退回内存）。
const userPickedTheme = ref(false);
const themeDark = ref(false);
const ping = ref<PingInfo | null>(null);
const pingError = ref("");
const backendVersion = ref(PLUGIN_VERSION);

function initTheme() {
  const saved = storageGet("usergw.theme");
  if (saved) {
    themeDark.value = saved === "dark";
    userPickedTheme.value = true;
    return;
  }
  // AstrBot 会把面板主题直接写在 <html data-theme="dark|light"> 上，这是最可靠的初值
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "dark" || attr === "light") {
    themeDark.value = attr === "dark";
    return;
  }
  const ctx = getContext();
  if (typeof ctx?.isDark === "boolean") themeDark.value = ctx.isDark;
}
initTheme();

// 面板切换主题 / 语言时会推送新 context
onContext((ctx) => {
  if (ctx.pageTitle) document.title = ctx.pageTitle;
  if (!userPickedTheme.value && typeof ctx.isDark === "boolean") themeDark.value = ctx.isDark;
});

const theme = computed(() => (themeDark.value ? darkTheme : null));
const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: "#7c5cff",
    primaryColorHover: "#8f73ff",
    primaryColorPressed: "#6a49f2",
    borderRadius: "8px",
  },
};

const menuOptions = [
  { label: () => h(RouterLink, { to: "/overview" }, { default: () => "总览" }), key: "/overview" },
  { label: () => h(RouterLink, { to: "/friends" }, { default: () => "私聊" }), key: "/friends" },
  { label: () => h(RouterLink, { to: "/groups" }, { default: () => "群聊" }), key: "/groups" },
  { label: () => h(RouterLink, { to: "/quota" }, { default: () => "限额" }), key: "/quota" },
  { label: () => h(RouterLink, { to: "/commands" }, { default: () => "指令" }), key: "/commands" },
  { label: () => h(RouterLink, { to: "/config" }, { default: () => "配置" }), key: "/config" },
];

const activeKey = computed(() => route.path);

/** 上次同步时间（0 表示还没同步过）。 */
const syncTime = computed(() => {
  const ts = ping.value?.sync?.last_at || 0;
  return ts ? new Date(ts * 1000).toLocaleString() : "";
});
const syncError = computed(() => ping.value?.sync?.error || "");

function toggleTheme() {
  themeDark.value = !themeDark.value;
  userPickedTheme.value = true;
  storageSet("usergw.theme", themeDark.value ? "dark" : "light");
}

async function pingBackend() {
  pingError.value = "";
  try {
    const info = await apiGet<PingInfo>("/ping");
    ping.value = info;
    backendVersion.value = info.version || PLUGIN_VERSION;
  } catch (e: any) {
    pingError.value = e?.message || String(e);
  }
}

onMounted(pingBackend);
</script>

<template>
  <n-config-provider :theme="theme" :theme-overrides="themeOverrides" :locale="zhCN" :date-locale="dateZhCN">
    <n-message-provider>
      <n-layout style="height: 100vh">
        <n-layout-header bordered style="padding: 12px 20px; display: flex; align-items: center; justify-content: space-between">
          <div style="display: flex; align-items: baseline; gap: 10px">
            <span style="font-size: 17px; font-weight: 600">萌萌权限控制台</span>
            <n-tag size="small" type="info" :bordered="false">v{{ backendVersion }}</n-tag>
            <n-tag v-if="ping && !ping.db_ready" size="small" type="error" :bordered="false">数据库未就绪</n-tag>
            <n-tag v-else-if="pingError" size="small" type="warning" :bordered="false">后端未连接</n-tag>
            <n-tooltip v-else-if="syncError" trigger="hover">
              <template #trigger>
                <n-tag size="small" type="warning" :bordered="false">同步失败</n-tag>
              </template>
              {{ syncError }}
            </n-tooltip>
            <n-tag v-else-if="syncTime" size="small" :bordered="false">同步于 {{ syncTime }}</n-tag>
          </div>
          <n-space align="center" :size="10">
            <n-button size="small" quaternary @click="pingBackend">刷新连接</n-button>
            <n-button size="small" quaternary @click="toggleTheme">{{ themeDark ? "浅色" : "深色" }}</n-button>
          </n-space>
        </n-layout-header>

        <n-layout has-sider position="absolute" style="top: 57px">
          <n-layout-sider bordered :width="180" :native-scrollbar="false">
            <n-menu :value="activeKey" :options="menuOptions" :root-indent="18" />
          </n-layout-sider>

          <n-layout-content content-style="padding: 18px 20px 28px" :native-scrollbar="false">
            <div v-if="pingError" style="margin-bottom: 14px">
              <n-tag type="warning" :bordered="false" style="white-space: normal">
                后端接口未连通：{{ pingError }}
              </n-tag>
            </div>
            <router-view />
          </n-layout-content>
        </n-layout>
      </n-layout>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
html,
body,
#app {
  height: 100%;
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}
</style>
