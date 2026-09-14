<script setup lang="ts">
// 权限状态标签：allow / deny / inherit（继承 = 走全局默认策略）
import { computed } from "vue";
import { NTag } from "naive-ui";

const props = defineProps<{ effect?: string }>();

const meta = computed(() => {
  switch (props.effect) {
    case "allow":
      return { type: "success" as const, text: "放行" };
    case "deny":
      return { type: "error" as const, text: "禁止" };
    default:
      return { type: "default" as const, text: "继承" };
  }
});
</script>

<template>
  <n-tag size="small" :type="meta.type" :bordered="false">{{ meta.text }}</n-tag>
</template>
