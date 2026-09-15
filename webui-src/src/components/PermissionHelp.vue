<script setup lang="ts">
// 权限说明卡（私聊 / 群聊两页共用）。
//
// 存在的理由：列表里「继承 / 放行 / 禁止」并排显示，很容易被看成「两个可以同时打开的
// 开关」（放行 + 禁止），于是不知道点哪个、以为它们冲突。这里用一句话讲清它们是
// **同一个开关的三个互斥状态**，并说明两列（LLM / 指令）是两套彼此独立的规则。
import { NCard, NSpace } from "naive-ui";

withDefaults(defineProps<{ kind?: "user" | "group" }>(), { kind: "user" });
</script>

<template>
  <n-card size="small" title="权限怎么算">
    <n-space vertical :size="4" style="font-size: 13px; opacity: 0.82">
      <span>
        · 「<b>继承 / 放行 / 禁止</b>」是<b>同一个开关的三个状态，互斥</b>，不是两个可以同时打开的开关：
        点一下就是「把这个对象设成这个状态」。「继承」= 不写专属规则、跟随上层。
      </span>
      <span>
        · <b>LLM 权限</b>与<b>指令权限</b>是两套彼此独立的规则：
        LLM 管「能不能用 AI 对话」，指令管「能不能用 <code>/xxx</code> 这类指令」，改一个不影响另一个。
      </span>
      <span>
        · 生效顺序（<b>命中最具体的一层即止</b>）：
        好友专属 → 好友等级 → {{ kind === "group" ? "群专属 → 群等级 → " : "" }}全局默认。
        列表里「等级」列改的是归级，「LLM 权限 / 指令权限」列才是这个对象自己的例外规则。
      </span>
      <span>
        · 「批量权限」下拉把 LLM 与指令<b>分成两组</b>，点哪一项就只改那一套规则。
      </span>
    </n-space>
  </n-card>
</template>
