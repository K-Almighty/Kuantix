<script setup lang="ts">
/**
 * 评级徽章（GradeBadge）：圆形大字母（S/A/B/C/D）+ 分数 + 颜色，悬停展示档位含义与否决原因。
 * 供 /backtest 单标的绩效 / /optimize 最优结果 / /compare 指标对比表复用。
 */
import { computed } from 'vue';
import { GRADE_META } from '../grading';
import type { GradeResult } from '../grading';

const props = withDefaults(
  defineProps<{
    result: GradeResult | null;
    /** sm 用于表格内紧凑展示，md/lg 用于报告顶部 */
    size?: 'sm' | 'md' | 'lg';
    /** 是否展示分数（如 "B 65.3"）。表格内通常关闭。 */
    showScore?: boolean;
  }>(),
  { size: 'md', showScore: true },
);

const meta = computed(() => (props.result ? GRADE_META[props.result.grade] : null));

const tooltip = computed(() => {
  if (!props.result || !meta.value) return '';
  const lines: string[] = [`${meta.value.grade} 档 · ${meta.value.hint}`];
  lines.push(`综合评分 ${props.result.score}`);
  if (props.result.insufficientSample) lines.push('⚠ 样本不足（<10 笔，胜率/利润因子已降权）');
  if (props.result.isLosing) lines.push('⚠ 系统亏损');
  for (const v of props.result.vetoes) {
    lines.push(`• ${v.reason}`);
  }
  return lines.join('\n');
});

const badgeClass = computed(() => [
  'grade-badge',
  `size-${props.size}`,
  props.result ? `grade-${props.result.grade}` : 'grade-none',
]);
</script>

<template>
  <span class="badge-wrapper">
    <span
      v-if="result && meta"
      :class="badgeClass"
      :style="{ '--grade-color': meta.color }"
      :title="tooltip"
      role="img"
      :aria-label="`评级 ${result.grade}：${meta.hint}`"
    >
      <span class="grade-letter">{{ result.grade }}</span>
      <span v-if="showScore" class="grade-score">{{ result.score.toFixed(0) }}</span>
    </span>
    <span v-else class="grade-badge grade-none" title="无评级数据">-</span>
    <span
      v-if="result && result.insufficientSample"
      class="sample-warn"
      title="交易笔数 < 10，胜率/利润因子已降权（不参与总分），评级基于净值类指标"
    >
      ⚠ 样本有限
    </span>
  </span>
</template>

<style scoped>
.badge-wrapper {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.sample-warn {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 6px;
  background: rgba(240, 160, 32, 0.12);
  border: 1px solid rgba(240, 160, 32, 0.45);
  color: var(--amber);
  font-weight: 500;
  white-space: nowrap;
  cursor: help;
}
.grade-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--grade-color) 18%, transparent);
  border: 1px solid color-mix(in srgb, var(--grade-color) 55%, transparent);
  color: var(--grade-color);
  font-weight: 700;
  line-height: 1;
  user-select: none;
  cursor: help;
  white-space: nowrap;
}
.grade-none {
  --grade-color: var(--gray);
  opacity: 0.6;
}
.grade-letter {
  font-size: 1em;
  letter-spacing: 0.5px;
}
.grade-score {
  font-size: 0.85em;
  opacity: 0.85;
  font-family: var(--mono);
  font-weight: 600;
}
.size-sm {
  font-size: 11px;
  padding: 1px 7px;
}
.size-md {
  font-size: 13px;
  padding: 3px 10px;
}
.size-lg {
  font-size: 16px;
  padding: 6px 14px;
}
.size-lg .grade-letter {
  font-size: 1.15em;
}
</style>
