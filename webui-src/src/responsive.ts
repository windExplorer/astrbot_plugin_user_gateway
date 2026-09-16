// 响应式工具：窄屏（≤768px）判断。
// 控制台跑在 AstrBot 面板的 sandbox iframe 里，宽度就是 iframe 宽度，
// matchMedia 在 iframe 内同样按视口宽度工作，手机上面板全屏时即为窄屏。
import { onMounted, onUnmounted, ref } from "vue";

export function useIsMobile() {
  const mq = window.matchMedia("(max-width: 768px)");
  const isMobile = ref(mq.matches);
  const onChange = (e: MediaQueryListEvent) => {
    isMobile.value = e.matches;
  };
  onMounted(() => mq.addEventListener("change", onChange));
  onUnmounted(() => mq.removeEventListener("change", onChange));
  return isMobile;
}
