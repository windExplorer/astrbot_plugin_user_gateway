import { createApp } from "vue";
import { createRouter, createWebHashHistory } from "vue-router";

import App from "./App.vue";
import OverviewView from "./views/OverviewView.vue";
import FriendsView from "./views/FriendsView.vue";
import GroupsView from "./views/GroupsView.vue";
import QuotaView from "./views/QuotaView.vue";
import ConfigView from "./views/ConfigView.vue";

// hash 路由：AstrBot 静态资源按真实文件路径解析，history 模式刷新会 404。
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    { path: "/overview", name: "overview", component: OverviewView },
    { path: "/friends", name: "friends", component: FriendsView },
    { path: "/groups", name: "groups", component: GroupsView },
    { path: "/quota", name: "quota", component: QuotaView },
    { path: "/config", name: "config", component: ConfigView },
    { path: "/:pathMatch(.*)*", redirect: "/overview" },
  ],
});

createApp(App).use(router).mount("#app");
