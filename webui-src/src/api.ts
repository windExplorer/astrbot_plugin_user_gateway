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
  /** 好友/群同步状态 */
  sync?: { last_at: number; running: boolean; ok: boolean; error: string };
  /** 内存中已加载的规则数量（确认改动是否真的生效） */
  rules?: {
    effect_users: number;
    effect_groups: number;
    levels?: number;
    leveled_users?: number;
    leveled_groups?: number;
    limits?: number;
    usage_users?: number;
    usage_groups?: number;
  };
  /** 头像缓存概况 */
  avatars?: Record<string, any>;
  /** 指令权限概况 */
  commands?: {
    enabled: boolean;
    default_effect: "allow" | "deny";
    priority: number;
    rules: number;
  };
  /** 模型路由概况（熔断中的提供商会列在这里） */
  model_route?: {
    enabled: boolean;
    levels: number;
    routed_sessions: number;
    circuit: Record<string, { until: number; remaining: number }>;
  };
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
  /** bot 最后消息概况（M2）：最近回过话的会话数与类型分布 */
  bot?: { kinds: { kind: string; cnt: number }[]; sessions: number; latest_ts: number };
}

/** 「当前生效」的额度档位（与后端闸门的档位链严格一致）。 */
export interface EffectiveQuota {
  layer: string;
  layer_label: string;
  scope_type: string;
  scope_id: string;
  period: string;
  limit: number;
  used: number;
  mode: string;
  exceeded?: boolean;
}

/** 额度档位链：从具体到兜底，用于详情抽屉解释「为什么按这条额度算」。 */
export interface QuotaChainItem {
  layer: string;
  label: string;
  scope_type: string;
  scope_id: string;
  limits: Record<string, { limit_tokens: number; mode: string; reset_at: number | null }>;
  usage: Record<string, number>;
  effective: boolean;
  exceeded: boolean;
}

/** bot 在某个会话里的最后一条消息。 */
export interface LastBotMessage {
  scope_type: string;
  scope_id: string;
  platform_id: string;
  ts: number;
  kind: string;
  command: string;
  preview: string;
}

export interface FriendRow {
  platform_id: string;
  uin: string;
  nickname: string;
  remark: string;
  display_name: string;
  avatar: string;
  avatar_id: string;
  effect: "allow" | "deny" | "inherit";
  /** 对象级「指令权限」（feature=command）的显式值 */
  effect_command: "allow" | "deny" | "inherit";
  level_id: number | null;
  level_name: string;
  quota: EffectiveQuota;
  quota_limit: number | null;
  quota_used: number | null;
  quota_mode: "enforce" | "observe" | null;
  quota_layer: string;
  today_tokens: number;
  last_bot_ts: number;
  last_bot_kind: string;
  last_bot_command: string;
  last_bot_preview: string;
  updated_at: number;
}

export interface GroupRow {
  platform_id: string;
  group_id: string;
  name: string;
  member_count: number;
  max_member_count: number;
  owner: string;
  avatar_id: string;
  effect: "allow" | "deny" | "inherit";
  /** 对象级「指令权限」（feature=command）的显式值 */
  effect_command: "allow" | "deny" | "inherit";
  level_id: number | null;
  level_name: string;
  quota: EffectiveQuota;
  quota_limit: number | null;
  quota_used: number | null;
  quota_mode: "enforce" | "observe" | null;
  quota_layer: string;
  today_tokens: number;
  last_bot_ts: number;
  last_bot_kind: string;
  last_bot_command: string;
  last_bot_preview: string;
  updated_at: number;
}

export interface Paged<T> {
  total: number;
  rows: T[];
  page: number;
  size: number;
  sort?: string;
}

/** 一条限额规则。``used_tokens``：对象专属 → 该对象用量；模板（level/global）→ null。 */
export interface QuotaRow {
  scope_type: "user" | "group" | "level" | "global";
  scope_id: string;
  period: "day" | "month" | "total";
  limit_tokens: number;
  used_tokens: number | null;
  mode: "enforce" | "observe";
  reset_at: number | null;
  updated_at: number;
}

/** 自定义等级（好友 / 群各一套）。 */
export interface LevelRow {
  id: number;
  kind: "user" | "group";
  name: string;
  description: string;
  effect: "inherit" | "allow" | "deny";
  /** 等级的默认**指令**权限（与 effect 分开配置） */
  effect_command: "inherit" | "allow" | "deny";
  sort_order: number;
  /** 模型路由：主提供商 id / 备用提供商 id（一项 = 一个「提供商 · 模型」） */
  provider_id: string;
  fallback_provider_id: string;
  members: number;
  quotas: Record<string, { limit_tokens: number; mode: string; reset_at: number | null }>;
}

/** 等级编辑负载：``quotas`` 里 ``limit_tokens=null`` 或 ``delete=true`` 表示删除该周期。 */
export interface LevelPayload {
  id?: number;
  kind: "user" | "group";
  name: string;
  description?: string;
  effect?: "inherit" | "allow" | "deny";
  effect_command?: "inherit" | "allow" | "deny";
  sort_order?: number;
  provider_id?: string;
  fallback_provider_id?: string;
  quotas?: { period: string; limit_tokens: number | null; mode?: string; delete?: boolean }[];
}

/** AstrBot 里已加载的对话模型提供商（一项 = 一个模型，对应一个提供商）。 */
export interface ProviderRow {
  id: string;
  /** 提供商（供应商）名 */
  name: string;
  /** 该提供商绑定的模型名 */
  model: string;
  /** 展示用：「名称 · 模型」 */
  label: string;
  type: string;
  modalities: string[];
  /** 是否是 AstrBot 当前默认的对话提供商 */
  is_default: boolean;
}

/** 某对象当前会走的模型（含来源等级与是否落到备用）。 */
export interface ModelRoute {
  layer?: string;
  label?: string;
  level_id?: number;
  provider_id?: string;
  fallback_provider_id?: string;
  used_fallback?: boolean;
  reason?: string;
  available?: string[];
  circuit_open?: string[];
}

export interface SubjectTotals {
  events: number;
  calls: number;
  denied: number;
  tok_in_other: number;
  tok_in_cached: number;
  tok_out: number;
  tok_total: number;
  estimated: number;
  avg_latency: number;
}

export interface SubjectDayPoint {
  day: string;
  tokens: number;
  calls: number;
  denied: number;
}

export interface SubjectDetail {
  type: "user" | "group";
  id: string;
  info: FriendRow | GroupRow | null;
  effect: "allow" | "deny" | "inherit";
  quotas: QuotaRow[];
  level_id: number | null;
  level: LevelRow | null;
  level_quotas: QuotaRow[];
  quota_chain: QuotaChainItem[];
  quota: EffectiveQuota;
  model_route: ModelRoute;
  /** 对象级指令权限：自己配的值 + 实际生效的层 */
  command_master: {
    effect: "allow" | "deny" | "inherit";
    resolved: "allow" | "deny";
    layer: string;
    layer_label: string;
    scope_type: string;
    scope_id: string;
  };
  usage: Record<string, { used_tokens: number; reset_at: number | null }>;
  bot: LastBotMessage | null;
  days: number;
  stats: {
    totals: SubjectTotals;
    series: SubjectDayPoint[];
    by_model: { model: string; tokens: number }[];
  };
  today: SubjectTotals;
  recent: Record<string, any>[];
}

/** 单个对象（好友/群）的详情：基础信息 + 权限 + 等级 + 额度档位链 + 区间用量与曲线。 */
export function apiSubject(type: "user" | "group", id: string, days = 7) {
  return apiGet<SubjectDetail>(`/subject?type=${type}&id=${encodeURIComponent(id)}&days=${days}`);
}

/** 手动同步好友 / 群列表（协议端往返，给足超时）。 */
export function apiSync() {
  return apiPost<{
    ok: boolean;
    total_friends: number;
    total_groups: number;
    platforms: { platform_id: string; ok: boolean; friends: number; groups: number; error?: string }[];
    error?: string;
  }>("/sync", {}, 60000);
}

/** 等级列表（含额度模板与成员数）。 */
/** 一条已注册的指令（AstrBot handler 注册表里带指令过滤器的处理器）。 */
export interface CommandRow {
  name: string;
  aliases: string[];
  desc: string;
  plugin: string;
  handler: string;
  is_group: boolean;
  /** 该指令的全局策略（inherit = 未配置，按「指令默认策略」走） */
  global_effect: "allow" | "deny" | "inherit";
  /** 例外规则（好友 / 群） */
  rules: { scope_type: "user" | "group"; scope_id: string; effect: "allow" | "deny" | "inherit" }[];
  rule_count: number;
}

export interface CommandsPayload {
  items: CommandRow[];
  default_effect: "allow" | "deny";
  enabled: boolean;
  priority: number;
  /** 规则里引用了但已经不存在（指令被卸载）的指令名 */
  stale: string[];
}

/** 指令清单 + 每条指令的权限规则。 */
export function apiCommands() {
  return apiGet<CommandsPayload>("/commands");
}

/** 清理已失效的指令规则（指令被卸载后残留）。 */
export function apiPruneCommands() {
  return apiPost<{ deleted: number; alive: number }>("/commands/prune", {});
}

export interface PolicyItem {
  scope_type: "user" | "group" | "global";
  scope_id: string;
  effect: "allow" | "deny" | "inherit";
  feature: string;
}

/** 批量写权限规则（LLM 用 feature="llm"，指令用 feature="command:<指令名>"）。 */
export function apiSetPolicy(items: PolicyItem[]) {
  return apiPost<{ applied: unknown[] }>("/policy", { items });
}

/** 读取某 feature 的规则。 */
export function apiGetPolicy(feature: string, scopeType?: "user" | "group") {
  const qs = `feature=${encodeURIComponent(feature)}${scopeType ? `&scope_type=${scopeType}` : ""}`;
  return apiGet<{ items: Record<string, any>[] }>(`/policy?${qs}`);
}

/** 可用的对话模型提供商（等级里选「走哪个模型」）。 */
export function apiProviders() {
  return apiGet<{ items: ProviderRow[]; circuit_open: string[]; route_enabled: boolean }>("/providers");
}

export function apiLevels(kind?: "user" | "group") {
  return apiGet<{ items: LevelRow[]; counts: Record<string, number> }>(
    `/levels${kind ? `?kind=${kind}` : ""}`,
  );
}

export function apiSetLevel(body: LevelPayload) {
  return apiPost<{ id: number }>("/levels", body, 20000);
}

export function apiDeleteLevel(id: number) {
  return apiPost<{ deleted: number }>("/levels/delete", { id });
}

/** 批量设置等级（``level_id=null`` 表示取消归级）。 */
export function apiSetSubjectLevel(
  items: { scope_type: "user" | "group"; scope_id: string; level_id: number | null }[],
) {
  return apiPost<{ applied: unknown[] }>("/subject-level", { items });
}

/** 批量取头像（返回 data URI 映射；只请求需要的 id，失败的不出现）。 */
export function apiAvatars(kind: "user" | "group", ids: string[], force = false) {
  const qs = `type=${kind}&ids=${encodeURIComponent(ids.join(","))}${force ? "&force=1" : ""}`;
  return apiGet<{ items: Record<string, string>; stats: Record<string, any>; got: number }>(
    `/avatars?${qs}`,
    45000,
  );
}

/** 强制更新头像缓存（``ids`` 省略 = 该类型全部已缓存的重取）。 */
export function apiRefreshAvatars(kind: "user" | "group", ids?: string[]) {
  return apiPost<{ refreshed: number; failed: number; stats: Record<string, any> }>(
    "/avatars/refresh",
    { type: kind, ids: ids && ids.length ? ids : undefined },
    120000,
  );
}

export interface ConfigPayload {
  items: Record<string, any>;
  schema: Record<string, any>;
}
