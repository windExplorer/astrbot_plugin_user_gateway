<script setup lang="ts">
// LLM 权限三态分段控件。
//
// 为什么是「一个分段控件」而不是「放行 / 禁止」两个按钮：这三者是**同一个开关的三个
// 互斥状态**（一个对象某一时刻只能处于其中一种），画成两个独立按钮会让人以为是两个
// 可以同时打开的开关。「继承」= 不写专属规则，跟随上层（等级 → 群专属 → 全局默认）。
import { NRadioButton, NRadioGroup, NTooltip } from "naive-ui";

const props = withDefaults(
  defineProps<{
    effect?: string;
    size?: "small" | "medium";
    disabled?: boolean;
  }>(),
  { effect: "inherit", size: "small", disabled: false },
);

const emit = defineEmits<{ (e: "change", value: string): void }>();

function onChange(v: string) {
  if (v !== props.effect) emit("change", v);
}
</script>

<template>
  <n-radio-group
    :value="props.effect || 'inherit'"
    :size="props.size"
    :disabled="props.disabled"
    @update:value="onChange"
  >
    <n-tooltip trigger="hover" :show-arrow="false">
      <template #trigger>
        <n-radio-button value="inherit">继承</n-radio-button>
      </template>
      不单独设规则，跟随上层（等级 → 群专属 → 全局默认）
    </n-tooltip>
    <n-tooltip trigger="hover" :show-arrow="false">
      <template #trigger>
        <n-radio-button value="allow">放行</n-radio-button>
      </template>
      显式允许（优先级高于等级与群规则）
    </n-tooltip>
    <n-tooltip trigger="hover" :show-arrow="false">
      <template #trigger>
        <n-radio-button value="deny">禁止</n-radio-button>
      </template>
      显式禁止（优先级最高，管理员豁免除外）
    </n-tooltip>
  </n-radio-group>
</template>
