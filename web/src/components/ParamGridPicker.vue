<script setup lang="ts">
/**
 * 寻优参数选择（ParamGridPicker）：从策略参数里勾选 1-2 个，各填取值列表（逗号分隔）。
 * - 切换策略时若有 preset_grid 预设网格则自动勾选并填入；否则清空。
 * - 网格点数 = 各参数取值数乘积，前端预校验 ≤ 200。
 */
import { computed, ref, watch } from 'vue';
import type { BacktestParamSchema, BacktestStrategySchema } from '../types';

const props = defineProps<{
  strategy: BacktestStrategySchema | null;
  /** 已选参数 → 候选值数组 */
  modelValue: Record<string, Array<number | string>>;
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', value: Record<string, Array<number | string>>): void;
}>();

/** 每个参数的取值输入框原始文本 */
const inputs = ref<Record<string, string>>({});
/** 勾选要寻优的参数（最多 2 个） */
const selected = ref<Set<string>>(new Set());

function parseValues(raw: string): Array<number | string> {
  return raw
    .split(/[,，\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => {
      const n = Number(s);
      return Number.isFinite(n) ? n : s;
    });
}

function syncOutputs(): void {
  const out: Record<string, Array<number | string>> = {};
  for (const name of selected.value) {
    const raw = inputs.value[name] ?? '';
    out[name] = parseValues(raw);
  }
  emit('update:modelValue', out);
}

function toggle(name: string): void {
  if (selected.value.has(name)) {
    selected.value.delete(name);
  } else {
    if (selected.value.size >= 2) return; // 最多 2 个
    selected.value.add(name);
  }
  selected.value = new Set(selected.value);
  syncOutputs();
}

function onInput(name: string, event: Event): void {
  inputs.value[name] = (event.target as HTMLInputElement).value;
  syncOutputs();
}

function presetToText(vals: Array<number | string>): string {
  return vals.join(', ');
}

// 切换策略时：若有预设网格则自动勾选并填入预设取值，否则清空选择
watch(
  () => props.strategy?.name,
  () => {
    const preset = props.strategy?.preset_grid;
    if (preset && Object.keys(preset).length > 0) {
      selected.value = new Set(Object.keys(preset));
      const newInputs: Record<string, string> = {};
      for (const n of Object.keys(preset)) {
        newInputs[n] = presetToText(preset[n]);
      }
      inputs.value = newInputs;
    } else {
      selected.value = new Set();
      inputs.value = {};
    }
    syncOutputs();
  },
  { immediate: true },
);

/** 网格点数（前端预校验） */
const gridPoints = computed(() => {
  const sizes = Array.from(selected.value).map((n) => parseValues(inputs.value[n] ?? '').length);
  return sizes.reduce((a, b) => a * b, 1);
});

const overLimit = computed(() => gridPoints.value > 200);

function isNumeric(p: BacktestParamSchema): boolean {
  return p.type === 'int' || p.type === 'float';
}
</script>

<template>
  <div class="grid-picker">
    <p class="hint">勾选 1-2 个参数作为寻优维度，各填候选取值（逗号分隔）。切换策略会自动填充预设网格。</p>
    <div v-if="strategy?.params.length" class="grid-params">
      <div v-for="p in strategy.params" :key="p.name" class="grid-param">
        <label class="grid-check">
          <input
            type="checkbox"
            :checked="selected.has(p.name)"
            :disabled="!selected.has(p.name) && selected.size >= 2"
            @change="toggle(p.name)"
          />
          <span class="grid-name">{{ p.label || p.name }}<span class="grid-key">（{{ p.name }}）</span></span>
        </label>
        <input
          v-if="isNumeric(p)"
          class="input grid-values"
          type="text"
          :value="inputs[p.name] ?? ''"
          :placeholder="`如 5, 10, 20（${p.type === 'int' ? '整数' : '小数'}）`"
          :disabled="!selected.has(p.name)"
          @input="onInput(p.name, $event)"
        />
        <input
          v-else
          class="input grid-values"
          type="text"
          :value="inputs[p.name] ?? ''"
          :placeholder="`逗号分隔取值`"
          :disabled="!selected.has(p.name)"
          @input="onInput(p.name, $event)"
        />
      </div>
    </div>
    <p v-else class="hint">当前策略无可寻优参数</p>

    <div class="grid-summary">
      <span class="grid-points" :class="{ over: overLimit }">网格点数：{{ gridPoints }}</span>
      <span v-if="gridPoints === 1" class="hint">（请至少为 1 个参数填写 ≥2 个取值）</span>
      <span v-else-if="overLimit" class="hint over">超过上限 200，请减少取值</span>
    </div>
  </div>
</template>

<style scoped>
.grid-picker {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.hint {
  color: var(--text-faint);
  font-size: 12px;
  margin: 0;
}
.grid-params {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.grid-param {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.grid-check {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  cursor: pointer;
}
.grid-name {
  font-weight: 500;
}
.grid-key {
  color: var(--text-faint);
  font-size: 11px;
}
.grid-values {
  width: 100%;
}
.grid-values:disabled {
  background: #f3f4f6;
  color: var(--text-faint);
}
.grid-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.grid-points {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
}
.grid-points.over,
.hint.over {
  color: var(--red);
}
</style>
