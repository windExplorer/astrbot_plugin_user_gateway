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
  darkTheme,
  zhCN,
  dateZhCN,
  NMessageProvider,
  type GlobalThemeOverrides,
} from "naive-ui";

import { apiGet, type PingInfo } from "./api";
import { PLUGIN_VERSION } from "./version";

const route = useRoute();

const themeDark = ref(localStorage.getItem("usergw.theme") === "dark");
const ping = ref<PingInfo | null>(null);
const pingError = ref("");
const backendVersion = ref(PLUGIN_VERSION);

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
  { label: () => h(RouterLink, { to: "/config" }, { default: () => "配置" }), key: "/config" },
];

const activeKey = computed(() => route.path);

function toggleTheme() {
  themeDark.value = !themeDark.value;
  localStorage.setItem("usergw.theme", themeDark.value ? "dark" : "light");
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
