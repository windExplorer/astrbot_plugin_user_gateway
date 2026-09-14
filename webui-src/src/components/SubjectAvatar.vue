<script setup lang="ts">
// QQ 头像：直接请求腾讯 CDN；失败或关闭头像加载时退化为「首字母 / 尾号」色块。
// 头像不入库、不经后端代理（离线环境下由用户在配置页关闭 avatars 开关）。
import { computed, ref } from "vue";

const props = defineProps<{
  qq: string;
  name?: string;
  size?: number;
  enabled?: boolean;
}>();

const failed = ref(false);

const size = computed(() => props.size || 34);

const src = computed(() => {
  if (props.enabled === false) return "";
  const q = String(props.qq || "").trim();
  return q ? `https://q1.qlogo.cn/g?b=qq&nk=${q}&s=100` : "";
});

const fallbackText = computed(() => {
  const n = (props.name || "").trim();
  if (n) return n.slice(0, 1).toUpperCase();
  const q = String(props.qq || "");
  return q ? q.slice(-2) : "?";
});

// 由 QQ 号派生的稳定色相，保证同一个人每次颜色一致
const bg = computed(() => {
  const q = String(props.qq || "");
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
