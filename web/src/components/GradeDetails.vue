<script setup lang="ts">
/**
 * 评级明细（GradeDetails）：各维度单项分（权重 × 分数）+ 一票否决原因列表。
 * 供 /backtest 单标的下钻、/optimize 最优结果展示「为什么是这个评级」。
 */
import { computed } from 'vue';
import { GRADE_META } from '../grading';
import type { GradeResult } from '../grading';

const props = withDefaults(
  defineProps<{
    result: GradeResult | null;
    /** 紧凑模式（表格/弹窗内） */
    compact?: boolean;
  }>(),
  { compact: false },
);

const meta = computed(() => (props.result ? GRADE_META[props.result.grade] : null));

function width(d: { score: number; weight: number }): string {
  // 展示宽度 = 权重 × 单项分（即该维度对总分的贡献占比）
  return `${Math.min(100, Math.max(2, d.score * d.weight)).toFixed(1)}%`;
}
</script>

<template>
  <div v-if="result" class="grade-details" :class="{ compact }">
    <div class="gd-head">
      <span class="gd-grade" :style="{ color: meta?.color }">{{ result.grade }}</span>
      <span class="gd-score">综合评分 {{ result.score }}</span>
      <span class="gd-hint">{{ meta?.hint }}</span>
    </div>

    <div class="gd-dims">
      <div v-for="d in result.dimensions" :key="d.key" class="gd-dim">
        <div class="gd-dim-head">
          <span class="gd-dim-label">{{ d.label }}</span>
          <span class="gd-dim-raw">{{ Number.isFinite(d.raw) ? d.raw.toFixed(3) : '-' }}</span>
          <span class="gd-dim-score">{{ d.score.toFixed(1) }}分</span>
          <span class="gd-dim-weight">权重{{ (d.weight * 100).toFixed(0) }}%</span>
        </div>
        <div class="gd-track">
          <div class="gd-fill" :style="{ width: width(d) }"></div>
        </div>
      </div>
    </div>

    <div v-if="result.vetoes.length" class="gd-vetoes">
      <div v-for="v in result.vetoes" :key="v.key" class="gd-veto">⚠ {{ v.reason }}</div>
    </div>
    <div v-if="result.insufficientSample" class="gd-note">
      ⚠ 交易样本不足（&lt;10 笔）：胜率/利润因子已降权，评级基于净值类指标。
    </div>
  </div>
  <div v-else class="grade-details empty">无评级数据</div>
</template>

<style scoped>
.grade-details {
  font-size: 12px;
}
.gd-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.gd-grade {
  font-size: 22px;
  font-weight: 800;
}
.gd-score {
  font-weight: 600;
}
.gd-hint {
  color: var(--text-secondary);
}
.gd-dims {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.gd-dim-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 11px;
}
.gd-dim-label {
  width: 72px;
  color: var(--text-secondary);
  flex: none;
}
.gd-dim-raw {
  font-family: var(--mono);
  color: var(--text-faint);
  flex: 1;
}
.gd-dim-score {
  font-family: var(--mono);
  font-weight: 600;
}
.gd-dim-weight {
  color: var(--text-faint);
  width: 52px;
  text-align: right;
}
.gd-track {
  height: 6px;
  background: #eef1f5;
  border-radius: 999px;
  overflow: hidden;
  margin-top: 2px;
}
.gd-fill {
  height: 100%;
  background: var(--primary);
  border-radius: 999px;
}
.gd-vetoes {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.gd-veto {
  color: var(--red);
  font-size: 11px;
  line-height: 1.4;
}
.gd-note {
  margin-top: 8px;
  color: var(--amber);
  font-size: 11px;
  line-height: 1.4;
}
.empty {
  color: var(--text-faint);
  padding: 8px 0;
}
.compact .gd-dim-raw,
.compact .gd-dim-weight {
  display: none;
}
</style>
