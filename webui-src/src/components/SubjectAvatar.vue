<script setup lang="ts">
// 头像：优先用后端缓存（data URI，见 avatarStore / avatar.py），没有就退回腾讯 CDN，
// 再失败（或配置关闭头像）退化为「首字母 / 尾号」色块 —— 三级兜底，任何环境都能看。
import { computed, ref } from "vue";

import { avatarOf } from "../avatarStore";

const props = withDefaults(
  defineProps<{
    kind?: "user" | "group";
    /** 好友 QQ 号或群号 */
    id?: string;
    /** 兼容旧用法（= id） */
    qq?: string;
    name?: string;
    size?: number;
    /** 配置页关掉头像时传 false */
    enabled?: boolean;
  }>(),
  { kind: "user", size: 34, enabled: true },
);

const failed = ref(false);

const size = computed(() => props.size || 34);
const targetId = computed(() => String(props.id || props.qq || "").trim());

// 后端缓存里有的直接用（离线也能显示）
const backendSrc = computed(() =>
  props.enabled === false ? "" : avatarOf(props.kind, targetId.value),
);
// 后端还没有 → 试一次腾讯 CDN（失败由 @error 兜底成色块）
const cdnSrc = computed(() => {
  if (props.enabled === false || !targetId.value) return "";
  const q = targetId.value;
  return props.kind === "group"
    ? `https://p.qlogo.cn/gh/${q}/${q}/100`
    : `https://q1.qlogo.cn/g?b=qq&nk=${q}&s=100`;
});
const src = computed(() => {
  failed.value = false;
  return backendSrc.value || cdnSrc.value;
});

const fallbackText = computed(() => {
  const n = (props.name || "").trim();
  if (n) return n.slice(0, 1).toUpperCase();
  const q = targetId.value;
  return q ? q.slice(-2) : "?";
});

// 由 id 派生的稳定色相，保证同一个人每次颜色一致
const bg = computed(() => {
  const q = targetId.value || (props.name || "");
  let h = 0;
  for (let i = 0; i < q.length; i++) h = (h * 31 + q.charCodeAt(i)) % 360;
  return `hsl(${h}, 52%, 62%)`;
});
</script>

<template>
  <img
    v-if="src && !failed"
    :src="src"
    :width="size"
    :height="size"
    loading="lazy"
    referrerpolicy="no-referrer"
    style="border-radius: 50%; object-fit: cover; display: block; background: rgba(128, 128, 128, 0.15)"
    @error="failed = true"
  />
  <div
    v-else
    :style="{
      width: size + 'px',
      height: size + 'px',
      borderRadius: '50%',
      background: bg,
      color: '#fff',
      fontSize: Math.round(size * 0.4) + 'px',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      fontWeight: 600,
      userSelect: 'none',
    }"
  >
    {{ fallbackText }}
  </div>
</template>
