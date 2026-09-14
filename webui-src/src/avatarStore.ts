// 头像的会话级缓存：列表页把当前页的 id 批量丢进来，组件里用 avatarOf() 取 data URI。
//
// 为什么走后端而不是让浏览器直连腾讯 CDN：见 avatar.py 的模块说明
// （控制台跑在 sandbox iframe 里，直连 CDN 在内网/离线/代理环境下整片挂掉；
//  后端抓取后还能做「更新头像」与离线兜底）。
import { reactive } from "vue";

import { apiAvatars } from "./api";

// Vue 3 的 reactive 对 Map 做了插桩，get/set/has 都是响应式的
const cache = reactive(new Map<string, string>());
// 在途请求去重：同一页几十行同时挂载只会发一次批量请求
const pending = new Set<string>();

export function avatarOf(kind: "user" | "group", id: string): string {
  return cache.get(`${kind}:${String(id || "").trim()}`) || "";
}

export function hasAvatar(kind: "user" | "group", id: string): boolean {
  return cache.has(`${kind}:${String(id || "").trim()}`);
}

/** 批量保证头像可用（missing 的才请求）。失败静默：头像取不到就退化为色块。 */
export async function loadAvatars(kind: "user" | "group", ids: (string | number)[], force = false): Promise<void> {
  const want: string[] = [];
  for (const raw of ids) {
    const id = String(raw || "").trim();
    if (!id) continue;
    const key = `${kind}:${id}`;
    if (force || (!cache.has(key) && !pending.has(key))) {
      want.push(id);
      pending.add(key);
    }
  }
  if (!want.length) return;
  try {
    const res = await apiAvatars(kind, want.slice(0, 400), force);
    for (const [id, uri] of Object.entries(res.items || {})) {
      if (uri) cache.set(`${kind}:${id}`, uri);
    }
  } catch {
    /* ignore：后端不可用时不影响列表本身的渲染 */
  } finally {
    for (const id of want) pending.delete(`${kind}:${id}`);
  }
}

/** 让缓存失效（「更新头像」后调用，下次渲染重新向后端要）。 */
export function dropAvatars(kind: "user" | "group", ids: (string | number)[]): void {
  for (const raw of ids) {
    const id = String(raw || "").trim();
    if (id) cache.delete(`${kind}:${id}`);
  }
}

export function avatarCacheSize(): number {
  return cache.size;
}
