// AstrBot 插件 Page 的官方 bridge 访问层（含 sandbox 环境的安全封装）。
//
// ⚠️ 关键环境约束（真实踩坑）：AstrBot 把插件页塞进 **sandbox iframe**：
//     <iframe sandbox="allow-scripts allow-forms allow-downloads">
//                                        ^ 没有 allow-same-origin
// 因此 `localStorage` / `sessionStorage` / `document.cookie` 一旦被访问就抛
// `SecurityError: ... document is sandboxed and lacks the 'allow-same-origin' flag`，
// 而且会发生在 Vue mount 阶段 —— 整个页面直接白屏。
// 参见 AstrBot 源码 `dashboard/src/views/PluginPagePage.vue` 的 iframe 属性。
//
// 所以本插件约定两条：
//   1) 任何持久化都走 storageGet/storageSet（内部 try/catch + 内存兜底）；
//   2) 主题 / 语言这类环境信息优先读 bridge 的 context（`isDark` / `locale`），
//      它由 AstrBot 面板下发，比自己存 localStorage 更正确。

export interface BridgeContext {
  pluginName?: string;
  displayName?: string;
  pageName?: string;
  pageTitle?: string;
  locale?: string;
  i18n?: Record<string, any>;
  isDark?: boolean;
}

export interface AstrBotPageBridge {
  ready?(): Promise<BridgeContext>;
  getContext?(): BridgeContext | null;
  onContext?(handler: (ctx: BridgeContext) => void): () => void;
  getLocale?(): string;
  t?(key: string, fallback?: string): string;
  apiGet(endpoint: string, params?: Record<string, any>): Promise<any>;
  apiPost(endpoint: string, body?: Record<string, any>): Promise<any>;
  download?(endpoint: string, params?: Record<string, any>, filename?: string): Promise<any>;
  upload?(endpoint: string, file: File): Promise<any>;
}

/** 取 iframe 内的 bridge 实例（由官方 bridge-sdk 注入到 window 上）。 */
export function getBridge(): AstrBotPageBridge | null {
  const w = window as any;
  if (w.AstrBotPluginPage) return w.AstrBotPluginPage as AstrBotPageBridge;
  // 极少数宿主会把页面直接内联在主文档里，此时 bridge 挂在父窗口。
  // 注意：sandbox iframe 里访问 window.parent 的属性会抛 SecurityError，必须捕获。
  try {
    if (w.parent && w.parent !== w && w.parent.AstrBotPluginPage) {
      return w.parent.AstrBotPluginPage as AstrBotPageBridge;
    }
  } catch {
    return null;
  }
  return null;
}

function isUsable(b: AstrBotPageBridge | null | undefined): b is AstrBotPageBridge {
  return Boolean(b && typeof b.apiGet === "function" && typeof b.apiPost === "function");
}

/** 等待 bridge 就绪（页面刚加载时 bridge 脚本可能还没执行完）。 */
export async function getPageBridge(timeoutMs = 3000): Promise<AstrBotPageBridge> {
  const start = Date.now();
  while (true) {
    const b = getBridge();
    if (isUsable(b)) return b;
    if (Date.now() - start > timeoutMs) {
      throw new Error("未检测到 AstrBot 插件 Page 桥接，请从 AstrBot 后台的插件拓展页打开本页面");
    }
    await new Promise((r) => setTimeout(r, 100));
  }
}

/** 当前面板下发的 context（可能为 null：bridge 未就绪或页面被独立打开）。 */
export function getContext(): BridgeContext | null {
  try {
    return getBridge()?.getContext?.() || null;
  } catch {
    return null;
  }
}

/** 订阅 context 变化（主题切换等）；已存在 context 时会立即回调一次。 */
export function onContext(handler: (ctx: BridgeContext) => void): () => void {
  try {
    const off = getBridge()?.onContext?.(handler);
    return typeof off === "function" ? off : () => {};
  } catch {
    return () => {};
  }
}

// ------------------------------------------------------------------ //
// sandbox 安全存储
// ------------------------------------------------------------------ //
const memory = new Map<string, string>();
let lsUsable: boolean | null = null;

function localStorageUsable(): boolean {
  if (lsUsable === null) {
    try {
      window.localStorage.getItem("__probe__");
      lsUsable = true;
    } catch {
      lsUsable = false;
    }
  }
  return lsUsable;
}

/** 读本地存储；sandbox iframe 中不可用时退回内存（本次会话内有效）。 */
export function storageGet(key: string): string | null {
  if (localStorageUsable()) {
    try {
      const v = window.localStorage.getItem(key);
      if (v !== null) return v;
    } catch {
      /* 落到内存 */
    }
  }
  return memory.has(key) ? (memory.get(key) as string) : null;
}

/** 写本地存储；不可用时只写内存，绝不抛异常。 */
export function storageSet(key: string, value: string): void {
  memory.set(key, value);
  if (!localStorageUsable()) return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* 忽略：内存里已经有了 */
  }
}
