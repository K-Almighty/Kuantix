<script setup lang="ts">
/**
 * 组合回测页 /portfolio（契约 v1.3 草案 P1–P3，P0）。
 * 语义：1 策略 × N 标的，总资金分仓（total_cash/N），combined_equity 按日期对齐金额求和。
 * 与 /backtest（归一化等权对比）并存，页面独立。
 * 左配置面板：标的池（D8 搜索多选）/ 策略 + 参数 / 时间 / 资金与成本；
 * 右报告面板：复用 PortfolioResultPanel（组合净值+回撤 / 指标卡 / 资金分配 / 各标的分表 / 导出）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useAppStore } from '../stores/app';
import { usePortfolioStore } from '../stores/portfolio';
import type {
  BacktestParamSchema,
  BacktestStrategySchema,
  PortfolioRunRequest,
} from '../types';
import type { SecurityHit } from '../types/data';
import { toastError, toastSuccess, toastWarning } from '../utils/toast';
import JobProgress from '../components/JobProgress.vue';
import PortfolioResultPanel from '../components/PortfolioResultPanel.vue';
import SecuritySearchBox from '../components/SecuritySearchBox.vue';
import StateBlock from '../components/StateBlock.vue';

const app = useAppStore();
const store = usePortfolioStore();

/* ---------- 标的池（1..20） ---------- */
const codes = ref<string[]>([]);

function onSearchSelect(hit: SecurityHit): void {
  if (codes.value.includes(hit.code)) {
    toastWarning(`标的 ${hit.code} 已在组合中`);
    return;
  }
  if (codes.value.length >= 20) {
    toastWarning('组合标的池上限 20 只');
    return;
  }
  codes.value.push(hit.code);
}

function removeCode(code: string): void {
  const idx = codes.value.indexOf(code);
  if (idx >= 0) codes.value.splice(idx, 1);
}

/* ---------- 策略 + 参数（复用 B1 schema 渲染，同 /backtest） ---------- */
const strategy = ref('ma_cross');
const paramValues = ref<Record<string, string | number | boolean>>({});

const selectedStrategy = computed<BacktestStrategySchema | undefined>(() =>
  store.strategies.find((s) => s.name === strategy.value),
);

function onStrategyChange(): void {
  const next: Record<string, string | number | boolean> = {};
  for (const p of selectedStrategy.value?.params ?? []) {
    const d = p.default;
    next[p.name] = d === null || d === undefined ? '' : (d as string | number | boolean);
  }
  paramValues.value = next;
}

function paramInputType(p: BacktestParamSchema): string {
  if (p.type === 'int' || p.type === 'float') return 'number';
  if (p.type === 'bool') return 'checkbox';
  return 'text';
}

function parseParam(p: BacktestParamSchema, raw: string | number | boolean): string | number | boolean {
  if (p.type === 'bool') return Boolean(raw);
  if (p.type === 'int') return Math.round(Number(raw));
  if (p.type === 'float') return Number(raw);
  return String(raw);
}

function onParamInput(p: BacktestParamSchema, event: Event): void {
  const el = event.target as HTMLInputElement;
  const value: string | number | boolean = p.type === 'bool' ? el.checked : el.value;
  paramValues.value[p.name] = parseParam(p, value);
}

/* ---------- 时间 / 资金与成本 ---------- */
const startDate = ref('2020-01-01');
const endDate = ref('2025-12-31');
const cash = ref(1000000);
const commission = ref(0.0003);
const minCommission = ref(5.0);
const stampTax = ref(0.001);
const slippage = ref(0);
const execution = ref<'next_open' | 'next_close'>('next_open');

const EXECUTIONS: { value: 'next_open' | 'next_close'; label: string }[] = [
  { value: 'next_open', label: '开盘价' },
  { value: 'next_close', label: '收盘价' },
];

/* ---------- 运行 ---------- */
function validate(): string | null {
  if (codes.value.length === 0) return '请先添加至少 1 只标的（组合回测 = 1 策略 × N 标的，资金分仓）';
  if (codes.value.length > 20) return '组合标的池上限 20 只';
  if (!strategy.value) return '请选择策略';
  if (!startDate.value || !endDate.value) return '请选择起止日期';
  if (startDate.value > endDate.value) return '起始日期不能晚于结束日期';
  if (!(cash.value > 0)) return '初始资金必须为正数';
  return null;
}

async function onRun(): Promise<void> {
  const invalid = validate();
  if (invalid) {
    toastWarning(invalid);
    return;
  }
  const req: PortfolioRunRequest = {
    market: app.market,
    codes: [...codes.value],
    strategy: strategy.value,
    params: { ...paramValues.value },
    start: startDate.value,
    end: endDate.value,
    cash: Number(cash.value),
    commission: Number(commission.value),
    min_commission: Number(minCommission.value),
    stamp_tax: Number(stampTax.value),
    slippage: Number(slippage.value),
    execution: execution.value,
  };
  try {
    await store.run(req);
    toastSuccess(`组合回测已提交：${req.strategy} · ${req.codes.length} 只标的 · 资金 ${Number(cash.value).toLocaleString('zh-CN')} 分仓`);
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

onMounted(() => {
  void store.loadStrategies().then(() => {
    onStrategyChange();
  });
});

onBeforeUnmount(() => {
  store.stopPolling();
});
</script>

<template>
  <div class="portfolio-page">
    <!-- 左栏：配置 -->
    <aside class="config-panel panel">
      <section class="config-section">
        <h3>标的池（{{ codes.length }}/20）</h3>
        <SecuritySearchBox :market="app.market" placeholder="搜索代码/名称添加标的，如 600000 / 浦发" @select="onSearchSelect" />
        <div v-if="codes.length" class="code-chips">
          <span v-for="c in codes" :key="c" class="code-chip">
            {{ c }}
            <button type="button" class="chip-x" title="移除" @click="removeCode(c)">×</button>
          </span>
        </div>
        <p v-else class="hint">至少添加 1 只标的（最多 20 只），总资金按 N 等分</p>
      </section>

      <section class="config-section">
        <h3>策略（1 策略 × N 标的）</h3>
        <StateBlock v-if="store.strategiesError && !app.dataLakeEmpty" state="error" :message="store.strategiesError" />
        <StateBlock v-else-if="store.strategiesError && app.dataLakeEmpty" state="empty" message="数据湖为空：请先同步行情数据（见顶部引导条），再加载策略" />
        <template v-else>
          <select v-model="strategy" class="input" @change="onStrategyChange">
            <option v-for="s in store.strategies" :key="s.name" :value="s.name">{{ s.label }}（{{ s.name }}）</option>
          </select>
          <p v-if="selectedStrategy" class="strategy-desc">{{ selectedStrategy.description }}</p>
          <div v-if="selectedStrategy?.params.length" class="param-grid">
            <label v-for="p in selectedStrategy.params" :key="p.name" class="param-field">
              <span class="param-label">{{ p.label || p.name }}</span>
              <input
                v-if="p.type === 'bool'"
                type="checkbox"
                :checked="Boolean(paramValues[p.name])"
                @change="onParamInput(p, $event)"
              />
              <input
                v-else
                class="input"
                :type="paramInputType(p)"
                :value="paramValues[p.name]"
                :min="p.min_value"
                :max="p.max_value"
                @input="onParamInput(p, $event)"
              />
            </label>
          </div>
        </template>
      </section>

      <section class="config-section">
        <h3>时间范围（日线）</h3>
        <div class="row">
          <label class="field"><span>起始</span><input v-model="startDate" class="input" type="date" /></label>
          <label class="field"><span>结束</span><input v-model="endDate" class="input" type="date" /></label>
        </div>
      </section>

      <section class="config-section">
        <h3>资金与成本</h3>
        <label class="field"><span>组合总资金（等分到 N 只）</span><input v-model.number="cash" class="input" type="number" min="1000" step="10000" /></label>
        <div class="row">
          <label class="field"><span>佣金率</span><input v-model.number="commission" class="input" type="number" min="0" step="0.0001" /></label>
          <label class="field"><span>最低佣金</span><input v-model.number="minCommission" class="input" type="number" min="0" step="0.1" /></label>
        </div>
        <div class="row">
          <label class="field"><span>印花税</span><input v-model.number="stampTax" class="input" type="number" min="0" step="0.0001" /></label>
          <label class="field"><span>滑点</span><input v-model.number="slippage" class="input" type="number" min="0" step="0.001" /></label>
        </div>
        <label class="field"><span>成交价</span>
          <select v-model="execution" class="input">
            <option v-for="e in EXECUTIONS" :key="e.value" :value="e.value">{{ e.label }}</option>
          </select>
        </label>
      </section>

      <button class="btn btn-primary run-btn" :disabled="store.running" @click="onRun">
        {{ store.running ? '组合回测运行中…' : '开始组合回测' }}
      </button>
    </aside>

    <!-- 右栏：报告 -->
    <main class="report-panel">
      <div v-if="store.resultError" class="error-banner">⚠ {{ store.resultError }}</div>
      <JobProgress v-if="store.job && store.job.status !== 'done' && store.job.status !== 'failed'" :job="store.job" />

      <div v-if="!store.result && !store.running && !store.resultError && !store.job" class="placeholder panel">
        <p v-if="app.dataLakeEmpty">数据湖为空：组合回测依赖本地行情数据，请先同步（见顶部引导条），再开始回测。</p>
        <p v-else>选择标的池与策略后点击「开始组合回测」：总资金按 N 等分、逐标的独立资金池跑，组合净值按日期对齐金额求和。</p>
      </div>

      <PortfolioResultPanel
        v-if="store.result"
        :result="store.result"
        title="组合回测结果（资金分仓）"
      />
    </main>
  </div>
</template>

<style scoped>
.portfolio-page {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.config-panel {
  width: 340px;
  flex-shrink: 0;
  padding: 16px;
}
.config-section {
  margin-bottom: 18px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.config-section:last-of-type {
  border-bottom: none;
  margin-bottom: 12px;
}
.config-section h3 {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 10px;
}
.hint {
  color: var(--text-faint);
  font-size: 12px;
  margin: 6px 0 0;
}
.strategy-desc {
  color: var(--text-secondary);
  font-size: 12px;
  margin: 8px 0 0;
  line-height: 1.5;
}
.code-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.code-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--primary-weak);
  color: var(--primary);
  border-radius: 6px;
  padding: 2px 8px;
  font-family: var(--mono);
  font-size: 12px;
}
.chip-x {
  border: none;
  background: transparent;
  color: var(--primary);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0 2px;
}
.chip-x:hover {
  color: var(--red);
}
.param-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 10px;
}
.param-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.param-label {
  font-size: 12px;
  color: var(--text-secondary);
}
.row {
  display: flex;
  gap: 8px;
}
.field {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--text-secondary);
}
.run-btn {
  width: 100%;
  margin-top: 4px;
}
.report-panel {
  flex: 1;
  min-width: 0;
}
.error-banner {
  background: var(--red-weak);
  border: 1px solid var(--red);
  color: var(--red);
  padding: 10px 14px;
  border-radius: var(--radius);
  margin-bottom: 12px;
  font-size: 13px;
}
.placeholder {
  padding: 40px;
  text-align: center;
  color: var(--text-faint);
}
</style>
