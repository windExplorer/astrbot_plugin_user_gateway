// API 封装：通过 AstrBot 官方插件 Page 桥接调用插件后端。
// 桥接会正确处理 /api/plug/ 路由与 asset_token 鉴权；**不能**用裸 fetch 相对路径（会 CORS 失败）。
//
// 后端统一返回信封 {code, data, message}：code !== 0 时抛错，页面只需 try/catch 展示 e.message。
//
// bridge 的发现/等待逻辑统一放在 ./bridge（那里同时处理 sandbox iframe 的存储限制）。

import { getPageBridge } from "./bridge";

const PAGE_PLUGIN_NAME = "astrbot_plugin_user_gateway";

// 不同 AstrBot 版本对端点前缀的处理略有差异，逐个候选尝试（首个成功即返回）。
function endpointCandidates(routePath: string): string[] {
  const clean = routePath.replace(/^\/+/, "");
  const list = [clean, "/" + clean, `${PAGE_PLUGIN_NAME}/${clean}`, `/${PAGE_PLUGIN_NAME}/${clean}`];
  return [...new Set(list.map((s) => s.replace(/\/+/g, "/")).filter(Boolean))];
}

function isRouteMissing(payload: any): boolean {
  if (!payload || typeof payload !== "object") return false;
  const text = String(payload.error || "") + " " + String(payload.message || "") + " " + String(payload.detail || "");
  return /未找到.*路由|route.*not.*found|not.*found.*route|404/i.test(text);
}

function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(label + " 超时（" + ms / 1000 + "s 无响应）")), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

/**
 * 解析后端返回。正常情况下 Dashboard 已经吃掉一层信封
 * （成功时只把 `data` 转发过来），这里做防御式解包：
 * 若仍是 `{status:"ok"|"error", data}` / `{code, data}` 形态则再脱一层。
 */
function unwrap(payload: any): any {
  if (payload && typeof payload === "object") {
    if (payload.status === "error") throw new Error(payload.message || "请求失败");
    if (payload.status === "ok" && "data" in payload) return payload.data;
    if (Object.prototype.hasOwnProperty.call(payload, "code")) {
      if (payload.code !== 0) throw new Error(payload.message || "请求失败");
      return payload.data;
    }
  }
  return payload;
}

/** 只有「路由不存在」才值得换下一种端点写法重试；业务错误必须立刻抛出。 */
function isRouteMissingError(e: any): boolean {
  if (isRouteMissing(e)) return true;
  const text = String(e?.message || e?.toString?.() || e || "");
  return /未找到.*路由|route.*not.*found|not.*found.*route|404|not found/i.test(text);
}

async function request(path: string, method: "GET" | "POST", body?: unknown, timeoutMs?: number): Promise<any> {
  const br = await getPageBridge();
  const url = new URL(path, "https://astrbot-plugin-page.local/");
  const candidates = endpointCandidates(url.pathname);
  const errors: string[] = [];
  const ms = timeoutMs ?? 15000;

  if (method === "GET") {
    const params = Object.fromEntries(url.searchParams.entries());
    for (const c of candidates) {
      try {
        const p = await withTimeout(br.apiGet(c, Object.keys(params).length ? params : undefined), ms, "GET " + c);
        if (isRouteMissing(p)) {
          errors.push(p.message || p.error || "未找到该路由");
          continue;
        }
        return unwrap(p);
      } catch (e: any) {
        // 业务错误（如参数校验失败）直接抛出，避免把同一个请求打到 4 种端点上
        if (!isRouteMissingError(e)) throw e;
        errors.push(e?.message || String(e));
      }
    }
    throw new Error(errors[0] || "未找到可用的页面 API 路由");
  }

  let payload = body || {};
  try {
    payload = JSON.parse(JSON.stringify(payload));
  } catch {
    /* ignore */
  }
  for (const c of candidates) {
    try {
      const r = await withTimeout(br.apiPost(c, payload), ms, "POST " + c);
      if (isRouteMissing(r)) {
        errors.push(r.message || r.error || "未找到该路由");
        continue;
      }
      return unwrap(r);
    } catch (e: any) {
      // 同上：POST 绝不能因为业务错误被重试多次（会出现重复写库）
      if (!isRouteMissingError(e)) throw e;
      errors.push(e?.message || e?.toString?.() || String(e));
    }
  }
  throw new Error(errors[0] || "未找到可用的页面 API 路由");
}

/** GET，endpoint 形如 "/friends"、"usage?page=1"。 */
export function apiGet<T = any>(endpoint: string, timeoutMs?: number): Promise<T> {
  return request(endpoint, "GET", undefined, timeoutMs) as Promise<T>;
}

/** POST，body 作为 JSON 负载。 */
export function apiPost<T = any>(endpoint: string, body?: unknown, timeoutMs?: number): Promise<T> {
  return request(endpoint, "POST", body || {}, timeoutMs) as Promise<T>;
}

// ---------------------------------------------------------------- 类型
export interface PingInfo {
  plugin: string;
  version: string;
  db_ready: boolean;
  data_dir: string;
  db_path: string;
  server_time: number;
}

export interface SummaryTotals {
  events: number;
  calls: number;
  denied: number;
  errors: number;
  tok_in_other: number;
  tok_in_cached: number;
  tok_out: number;
  tok_total: number;
  estimated: number;
  users: number;
  groups: number;
  avg_latency: number;
}

export interface SummaryData {
  range: { from: number; to: number };
  range_key: string;
  totals: SummaryTotals;
  trend: { day: string; tokens: number; calls: number; denied: number }[];
  top_scopes: { scope_type: string; scope_id: string; tokens: number; events: number }[];
  by_model: { model: string; tokens: number }[];
  deny_reasons: { reason: string; cnt: number }[];
}

export interface FriendRow {
  platform_id: string;
  uin: string;
  nickname: string;
  remark: string;
  display_name: string;
  avatar: string;
  effect: "allow" | "deny" | "inherit";
  quota_limit: number | null;
  quota_used: number | null;
  quota_mode: "enforce" | "observe" | null;
  updated_at: number;
}

export interface GroupRow {
  platform_id: string;
  group_id: string;
  name: string;
  member_count: number;
  max_member_count: number;
  owner: string;
  effect: "allow" | "deny" | "inherit";
  quota_limit: number | null;
  quota_used: number | null;
  quota_mode: "enforce" | "observe" | null;
  updated_at: number;
}

export interface Paged<T> {
  total: number;
  rows: T[];
  page: number;
  size: number;
}

export interface QuotaRow {
  scope_type: string;
  scope_id: string;
  period: "day" | "month" | "total";
  limit_tokens: number;
  used_tokens: number;
  mode: "enforce" | "observe";
  reset_at: number | null;
  updated_at: number;
}

export interface ConfigPayload {
  items: Record<string, any>;
  schema: Record<string, any>;
}
