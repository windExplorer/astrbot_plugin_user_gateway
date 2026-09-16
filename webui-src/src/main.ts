import { createApp } from "vue";
import { createRouter, createWebHashHistory } from "vue-router";

import App from "./App.vue";
import OverviewView from "./views/OverviewView.vue";
import StatsView from "./views/StatsView.vue";
import FriendsView from "./views/FriendsView.vue";
import GroupsView from "./views/GroupsView.vue";
import QuotaView from "./views/QuotaView.vue";
import CommandsView from "./views/CommandsView.vue";
import ConfigView from "./views/ConfigView.vue";

// hash 路由：AstrBot 静态资源按真实文件路径解析，history 模式刷新会 404。
// 注意：必须保持同步导入 —— vite.config 里 inlineDynamicImports 是有意为之
// （多 chunk 会被 AstrBot 的 asset_token 重写搞成 401 白屏），懒加载在这里没有收益。
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    { path: "/overview", name: "overview", component: OverviewView },
    { path: "/stats", name: "stats", component: StatsView },
    { path: "/friends", name: "friends", component: FriendsView },
    { path: "/groups", name: "groups", component: GroupsView },
    { path: "/quota", name: "quota", component: QuotaView },
    { path: "/commands", name: "commands", component: CommandsView },
    { path: "/config", name: "config", component: ConfigView },
    { path: "/:pathMatch(.*)*", redirect: "/overview" },
  ],
});

createApp(App).use(router).mount("#app");
