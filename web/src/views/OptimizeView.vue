<script setup lang="ts">
/**
 * 参数寻优页 /optimize（契约 v1.3 草案 O1–O3，docs/06 §2.3）。
 * 左配置：单标的（D8 搜索单选）/ 策略（B1 下拉 + ParamGridPicker 1-2 参数网格 ≤200）/ 日期 / 资金成本
 * 右报告：best 参数 + 绩效指标卡 + 寻优评级徽章 + 排名表 + 热力图（2 参数）或折线/柱状（单参数）+ 导出 JSON。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import type { EChartsCoreOption } from 'echarts/core';
import { api } from '../api';
import { useAppStore } from '../stores/app';
import { useBacktestStore } from '../stores/backtest';
import { useOptimizeStore } from '../stores/optimize';
import type {
  BacktestStrategySchema,
  OptimizeAllRankEntry,
  OptimizeGridPoint,
  OptimizeRunRequest,
} from '../types';
import type { SecurityHit } from '../types/data';
import { gradeGridPoint } from '../grading';
import { fmtInt, fmtNumber, fmtPct, fmtSignedPct } from '../utils/format';
import { toastError, toastSuccess, toastWarning } from '../utils/toast';
import EChart from '../components/EChart.vue';
import ExportButton from '../components/ExportButton.vue';
import GradeBadge from '../components/GradeBadge.vue';
import GradeDetails from '../components/GradeDetails.vue';
import JobProgress from '../components/JobProgress.vue';
import ParamGridPicker from '../components/ParamGridPicker.vue';
import SecuritySearchBox from '../components/SecuritySearchBox.vue';
import StateBlock from '../components/StateBlock.vue';

const app = useAppStore();
const store = useOptimizeStore();
const backtestStore = useBacktestStore();
const router = useRouter();

/* ---------- 标的（单选） ---------- */
const code = ref('');
const codeName = ref('');
function onSearchSelect(hit: SecurityHit): void {
  code.value = hit.code;
  codeName.value = hit.name;
}
function clearCode(): void {
  code.value = '';
  codeName.value = '';
}

/* ---------- 策略 + 寻优网格 ---------- */
const strategy = ref('');
const paramGrid = ref<Record<string, Array<number | string>>>({});

const selectedStrategy = computed<BacktestStrategySchema | undefined>(() =>
  store.schemas.find((s) => s.name === strategy.value),
);

function onStrategyChange(): void {
  // ParamGridPicker 内部 watch strategy.name 自动填充 preset_grid / 清空
  paramGrid.value = {};
}

/* ---------- 日期 / 资金 / 成本 ---------- */
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

/* ---------- 网格预校验 ---------- */
const gridPoints = computed(() => {
  const sizes = Object.values(paramGrid.value).map((v) => v.length);
  return sizes.reduce((a, b) => a * b, 1);
});

function validate(): string | null {
  if (!code.value) return '请选择 1 只标的（搜索代码/名称）';
  if (!strategy.value) return '请选择策略';
  const names = Object.keys(paramGrid.value);
  if (names.length === 0) return '请勾选至少 1 个参数并填写候选取值';
  if (names.length > 2) return '最多勾选 2 个参数';
  for (const n of names) {
    if ((paramGrid.value[n]?.length ?? 0) < 2) return `参数「${n}」请至少填写 2 个候选取值`;
  }
  if (gridPoints.value > 200) return `网格点数 ${gridPoints.value} 超过上限 200`;
  if (!startDate.value || !endDate.value) return '请选择起止日期';
  if (startDate.value > endDate.value) return '起始日期不能晚于结束日期';
  if (!(cash.value > 0)) return '初始资金必须为正数';
  return null;
}

/* ---------- 运行（O1 → O2 轮询 → O3） ---------- */
async function onRun(): Promise<void> {
  const invalid = validate();
  if (invalid) {
    toastWarning(invalid);
    return;
  }
  const req: OptimizeRunRequest = {
    market: app.market,
    code: code.value,
    strategy: strategy.value,
    param_grid: { ...paramGrid.value },
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
    toastSuccess(`寻优已提交：${req.strategy} · ${req.code} · ${gridPoints.value} 个网格点`);
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

/* ---------- 复制最优参数（F3 增强：便于粘贴到回测表单） ---------- */
const copying = ref(false);
async function copyBestParams(): Promise<void> {
  const best = store.result?.best;
  if (!best) {
    toastWarning('没有可复制的最优参数');
    return;
  }
  const text = JSON.stringify(best.params, null, 2);
  copying.value = true;
  try {
    await navigator.clipboard.writeText(text);
    toastSuccess('已复制最优参数到剪贴板');
  } catch {
    // 剪贴板不可用时降级为弹窗展示
    window.prompt('复制以下最优参数：', text);
  } finally {
    copying.value = false;
  }
}

/* ---------- 删除单个策略寻优（O6） ---------- */
const deleting = ref(false);
async function onDeleteJob(jobId?: string): Promise<void> {
  if (!jobId) {
    toastWarning('没有可删除的寻优任务');
    return;
  }
  if (!window.confirm('确定删除该次寻优（任务记录与完整结果）？此操作不可恢复。')) {
    return;
  }
  deleting.value = true;
  try {
    await store.remove(jobId);
    toastSuccess('已删除该次寻优');
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  } finally {
    deleting.value = false;
  }
}

/* ---------- 结果渲染 ---------- */
const bestGrade = computed(() => (store.result?.best ? gradeGridPoint(store.result.best) : null));

const bestMetricCards = computed(() => {
  const best = store.result?.best;
  if (!best) return [];
  return [
    { key: 'total_return', label: '总收益率', value: best.total_return, fmt: fmtSignedPct },
    { key: 'sharpe', label: '夏普比率', value: best.sharpe, fmt: fmtNumber },
    { key: 'max_drawdown', label: '最大回撤', value: best.max_drawdown, fmt: fmtPct },
    { key: 'win_rate', label: '胜率', value: best.win_rate, fmt: fmtPct },
    { key: 'profit_factor', label: '利润因子', value: best.profit_factor, fmt: fmtNumber },
    { key: 'total_trades', label: '成交笔数', value: best.total_trades, fmt: fmtInt },
  ];
});

function paramText(params: Record<string, number | string>): string {
  return Object.entries(params)
    .map(([k, v]) => `${k}=${v}`)
    .join(' · ');
}

/* 热力图（2 参数，契约 §3.8）：x=param0 取值，y=param1 取值，data 为稀疏三元组 [x_idx, y_idx, value] */
const heatmapOption = computed<EChartsCoreOption>(() => {
  const h = store.result?.heatmap;
  const names = store.result?.param_names ?? [];
  if (!h || !h.x.length || !h.y.length) return {};
  // 稀疏三元组 [x_idx, y_idx, value] 即 ECharts heatmap 原生数据格式
  const data = (h.data ?? []).filter((d): d is [number, number, number] => d[2] !== null);
  const values = data.map((d) => d[2]);
  const vmin = values.length > 0 ? Math.min(...values) : 0;
  const vmax = values.length > 0 ? Math.max(...values) : 1;
  const xName = h.x_name || names[0] || '参数1';
  const yName = h.y_name || names[1] || '参数2';
  return {
    tooltip: {
      position: 'top',
      formatter: (p: { value: [number, number, number | null] }) => {
        const [xi, yi, v] = p.value;
        return `${xName}=${h.x[xi]} · ${yName}=${h.y[yi]}<br/>总收益 ${v === null ? '-' : fmtSignedPct(v)}`;
      },
    },
    grid: { left: 90, right: 30, top: 24, bottom: 60 },
    xAxis: { type: 'category', data: h.x.map((v) => String(v)), name: xName, splitArea: { show: true } },
    yAxis: { type: 'category', data: h.y.map((v) => String(v)), name: yName, splitArea: { show: true } },
    visualMap: {
      min: vmin,
      max: vmax,
      calculable: true,
      orient: 'horizontal',
      left: 'center',
      bottom: 4,
      inRange: { color: ['#fee2e2', '#fca5a5', '#f97316', '#facc15', '#22c55e', '#15803d'] },
    },
    series: [
      {
        name: '总收益',
        type: 'heatmap',
        data,
        label: {
          show: true,
          formatter: (p: { value: [number, number, number | null] }) =>
            p.value[2] === null ? '-' : (p.value[2]! * 100).toFixed(1) + '%',
        },
        itemStyle: { borderWidth: 1, borderColor: '#fff' },
        emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.4)' } },
      },
    ],
  };
});

/* 单参数：折线/柱状（按参数值升序） */
const singleParamOption = computed<EChartsCoreOption>(() => {
  const result = store.result;
  const names = result?.param_names ?? [];
  if (!result || names.length !== 1) return {};
  const pname = names[0];
  const ordered = [...result.results].sort((a, b) => {
    const av = Number(a.params[pname]);
    const bv = Number(b.params[pname]);
    return av - bv;
  });
  const xs = ordered.map((r) => String(r.params[pname]));
  const ys = ordered.map((r) => r.total_return);
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 24, top: 30, bottom: 40 },
    xAxis: { type: 'category', data: xs, name: pname },
    yAxis: { type: 'value', name: '总收益', scale: true },
    series: [
      {
        name: '总收益',
        type: 'bar',
        data: ys,
        itemStyle: { color: (p: { value: number }) => (p.value >= 0 ? '#16a34a' : '#dc2626') },
      },
    ],
  };
});

/* 排名表（按 total_return 降序，前 20） */
const rankedResults = computed(() => (store.result?.results ?? []).slice(0, 20));

async function exportOptimize(): Promise<{ blob: Blob; filename: string }> {
  const result = store.result;
  if (!result) throw new Error('无寻优结果可导出');
  return {
    blob: new Blob([JSON.stringify(result, null, 2)], { type: 'application/json; charset=utf-8' }),
    filename: `optimize_${result.strategy}_${code.value || 'unknown'}.json`,
  };
}

/* ---------- 一键寻优所有策略（O4/O5） ---------- */
// 并发工作进程数：0=串行；2+=多进程并行（CPU-bound 必须）。默认 8，用户改过
// 则记到 localStorage，下次以最后一次选择为默认（与回测单页一致）。
const cpuCount = (() => {
  const n = typeof navigator !== 'undefined' ? navigator.hardwareConcurrency : undefined;
  return n && n > 0 ? n : 4;
})();
const WORKER_OPTIONS = [
  { value: 0, label: '串行（不并发）' },
  { value: 4, label: '4 进程' },
  { value: 8, label: '8 进程' },
  { value: 16, label: '16 进程' },
];
const WORKERS_STORAGE_KEY = 'optimize.workers';
const DEFAULT_WORKERS = Math.min(cpuCount, 8);
function loadWorkersFromStorage(): number {
  try {
    const raw = localStorage.getItem(WORKERS_STORAGE_KEY);
    if (raw == null) return DEFAULT_WORKERS;
    const n = Number(raw);
    return Number.isFinite(n) && WORKER_OPTIONS.some((o) => o.value === n) ? n : DEFAULT_WORKERS;
  } catch {
    return DEFAULT_WORKERS;
  }
}
const workers = ref(loadWorkersFromStorage());
watch(workers, (v) => {
  try {
    localStorage.setItem(WORKERS_STORAGE_KEY, String(v));
  } catch {
    /* localStorage 不可用时静默忽略 */
  }
});

async function onRunAll(): Promise<void> {
  if (!code.value) {
    toastWarning('请先选择 1 只标的（搜索代码/名称）');
    return;
  }
  try {
    await store.runAll({
      market: app.market,
      code: code.value,
      start: startDate.value,
      end: endDate.value,
      cash: cash.value,
      commission: commission.value,
      min_commission: minCommission.value,
      stamp_tax: stampTax.value,
      slippage: slippage.value,
      execution: execution.value,
      workers: workers.value,
    });
    toastSuccess('一键寻优所有策略已提交，正在计算…');
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

const allRanking = computed(() => store.allResult?.ranking ?? []);
const allBest = computed(() => store.allResult?.best);
const bestAllGrade = computed(() => (store.allResult?.best ? gradeGridPoint(store.allResult.best) : null));

/* 策略全局排名：总收益率条形图（降序） */
const allRankBarOption = computed<EChartsCoreOption>(() => {
  const items = allRanking.value;
  if (!items.length) return {};
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 100, right: 30, top: 16, bottom: 24 },
    xAxis: { type: 'value', name: '总收益率', formatter: (v: number) => `${(v * 100).toFixed(1)}%` },
    yAxis: { type: 'category', data: items.map((i) => i.strategy_label || i.strategy), inverse: true },
    series: [
      {
        name: '总收益率',
        type: 'bar',
        data: items.map((i) => i.total_return ?? null),
        label: { show: true, position: 'right', formatter: (p: { value: number }) => (p.value == null ? '-' : `${(p.value * 100).toFixed(1)}%`) },
        itemStyle: {
          color: (p: { value: number }) => (p.value >= 0 ? '#16a34a' : '#ef4444'),
        },
        barMaxWidth: 16,
      },
    ],
  };
});

/**
 * OptimizeAllRankEntry 中可格式化为数值的字段。
 * 单列出来是因为该接口还有一个对象型字段 params（Record<string, number | string>），
 * 用 { [k: string]: number | string | null } 这类索引签名做形参会把 params 也算进去，
 * 导致整个 OptimizeAllRankEntry 不可赋值（TS2345）。
 */
type OptimizeAllRankMetric =
  | 'total_return'
  | 'annual_return'
  | 'sharpe'
  | 'max_drawdown'
  | 'total_trades'
  | 'win_rate'
  | 'profit_factor'
  | 'grid_points';

function fmtAllEntry(it: OptimizeAllRankEntry, key: OptimizeAllRankMetric): string {
  const v = it[key];
  if (v === null || v === undefined) return '-';
  const n = Number(v);
  if (!Number.isFinite(n)) return '-';
  if (key === 'total_return' || key === 'annual_return' || key === 'max_drawdown' || key === 'win_rate' || key === 'profit_factor') {
    return fmtSignedPct(n);
  }
  return fmtInt(n);
}

function fmtAllParams(it: { params?: Record<string, number | string> }): string {
  if (!it.params) return '-';
  return Object.entries(it.params)
    .map(([k, v]) => `${k}=${v}`)
    .join(', ');
}

/** 一键寻优排名「查看」：把该策略+参数预填到回测页，并自动跑一次回测直接展示交易明细 */
function onViewAll(strategyName: string, params: Record<string, number | string>): void {
  backtestStore.setPreset({
    strategy: strategyName,
    params,
    codes: code.value ? [code.value] : [],
    startDate: startDate.value,
    endDate: endDate.value,
    autoRun: true,
  });
  router.push({ path: '/backtest' });
}

onMounted(() => {
  void store.loadStrategies().then(() => {
    if (store.schemas.length > 0 && !strategy.value) {
      strategy.value = store.schemas[0].name;
    }
  });
});

onBeforeUnmount(() => {
  store.stopPolling();
  store.stopAllPolling();
});
</script>

<template>
  <div class="optimize-page">
    <!-- 左栏：配置 -->
    <aside class="config-panel panel">
      <section class="config-section">
        <h3>标的（单选）</h3>
        <SecuritySearchBox :market="app.market" placeholder="搜索代码/名称，如 600519 / 贵州茅台" @select="onSearchSelect" />
        <div v-if="code" class="code-chips">
          <span class="code-chip">
            {{ code }} {{ codeName }}
            <button type="button" class="chip-x" title="清除" @click="clearCode">×</button>
          </span>
        </div>
        <p v-else class="hint">寻优为单标的模式，请选择 1 只标的</p>
      </section>

      <section class="config-section">
        <h3>策略</h3>
        <StateBlock v-if="store.schemasError" state="error" :message="store.schemasError" />
        <template v-else>
          <select v-model="strategy" class="input strategy-select" @change="onStrategyChange">
            <option v-for="s in store.schemas" :key="s.name" :value="s.name">{{ s.label }}（{{ s.name }}）</option>
          </select>
          <p v-if="selectedStrategy" class="strategy-desc">{{ selectedStrategy.description }}</p>
        </template>
      </section>

      <section class="config-section">
        <h3>寻优参数（1-2 个）</h3>
        <ParamGridPicker v-model="paramGrid" :strategy="selectedStrategy ?? null" />
      </section>

      <section class="config-section">
        <h3>时间范围</h3>
        <div class="row">
          <label class="field"><span>起始</span><input v-model="startDate" class="input" type="date" /></label>
          <label class="field"><span>结束</span><input v-model="endDate" class="input" type="date" /></label>
        </div>
      </section>

      <section class="config-section">
        <h3>资金与成本</h3>
        <label class="field"><span>初始资金</span><input v-model.number="cash" class="input" type="number" min="1000" step="10000" /></label>
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
        {{ store.running ? '寻优运行中…' : '开始寻优' }}
      </button>

      <section class="config-section rank-section">
        <h3>一键寻优所有策略</h3>
        <p class="hint">对全部已注册策略的预设参数网格逐策略寻优，汇总成全局策略排名，找出最优策略 + 参数组合。</p>
        <label class="field">
          <span>工作进程（CPU {{ cpuCount }} 核 · 推荐 {{ DEFAULT_WORKERS }}）</span>
          <select v-model.number="workers" class="input">
            <option v-for="o in WORKER_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </label>
        <p class="workers-tip">寻优是 CPU 密集计算，多进程可显著提速（仅对「一键寻优所有策略」生效）。机器较弱时建议先用串行测一次再开并发。</p>
        <button
          class="btn run-btn run-all-btn"
          :disabled="store.allRunning || !code"
          @click="onRunAll"
        >
          {{ store.allRunning ? '一键寻优运行中…' : '一键寻优所有策略' }}
        </button>
      </section>
    </aside>

    <!-- 右栏：报告 -->
    <main class="report-panel">
      <div v-if="store.resultError" class="error-banner">⚠ {{ store.resultError }}</div>
      <div v-if="store.allError" class="error-banner">⚠ {{ store.allError }}</div>
      <JobProgress v-if="store.job && store.job.status !== 'done' && store.job.status !== 'failed'" :job="store.job" />
      <JobProgress v-if="store.allJob && store.allJob.status !== 'done' && store.allJob.status !== 'failed'" :job="store.allJob" />

      <div v-if="!store.result && !store.allResult && !store.running && !store.allRunning && !store.resultError && !store.job && !store.allJob" class="placeholder panel">
        <p v-if="app.dataLakeEmpty">数据湖为空：寻优依赖本地行情数据，请先同步（见顶部引导条）。</p>
        <p v-else>选标的 → 选策略 → 勾选寻优参数 → 点「开始寻优」；或直接点「一键寻优所有策略」对全部策略预设网格寻优并全局排名。</p>
      </div>

      <div v-if="store.allResult" class="report-content">
        <div class="result-toolbar">
          <span class="result-title">
            一键寻优所有策略 · {{ store.allResult.code }}
            <template v-if="store.allResult.start_date">· {{ store.allResult.start_date }} ~ {{ store.allResult.end_date }}</template>
            · {{ store.allResult.total_strategies }} 个策略 / {{ store.allResult.total_grid_points }} 个网格点
          </span>
          <div class="toolbar-actions">
            <button
              type="button"
              class="btn btn-danger btn-sm"
              :disabled="deleting"
              title="删除该次一键寻优（任务记录与完整结果）"
              @click="onDeleteJob(store.allJob?.job_id)"
            >
              {{ deleting ? '删除中…' : '删除此寻优' }}
            </button>
          </div>
        </div>

        <section v-if="allBest" class="report-section panel">
          <h3>全局最佳</h3>
          <div class="best-block">
            <div class="best-params">
              <div class="best-label">最佳策略</div>
              <div class="best-value">{{ allBest.strategy_label || allBest.strategy }}（{{ allBest.strategy }}）</div>
            </div>
            <div class="best-params">
              <div class="best-label">最优参数</div>
              <div class="best-value">{{ fmtAllParams(allBest) }}</div>
            </div>
            <div class="best-grade">
              <div class="best-label">寻优评级</div>
              <GradeBadge :result="bestAllGrade" size="md" />
            </div>
          </div>
          <div class="metric-cards">
            <div class="metric-card">
              <div class="metric-label">总收益率</div>
              <div class="metric-value">{{ fmtSignedPct(allBest.total_return ?? 0) }}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">夏普比率</div>
              <div class="metric-value">{{ fmtNumber(allBest.sharpe ?? 0) }}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">最大回撤</div>
              <div class="metric-value">{{ fmtPct(allBest.max_drawdown ?? 0) }}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">胜率</div>
              <div class="metric-value">{{ fmtPct(allBest.win_rate ?? 0) }}</div>
            </div>
          </div>
          <p class="meta-line">共 {{ allRanking.length }} 个策略有效 · 合计 {{ store.allResult?.total_grid_points ?? 0 }} 网格点</p>
        </section>

        <section class="report-section panel">
          <h3>策略全局排名（按总收益降序）</h3>
          <EChart :option="allRankBarOption" height="420px" />
        </section>

        <section class="report-section panel">
          <h3>策略回测绩效排名表</h3>
          <div class="table-wrap">
            <table class="tbl">
              <thead>
                <tr>
                  <th>#</th>
                  <th>策略</th>
                  <th>最优参数</th>
                  <th>总收益</th>
                  <th>年化</th>
                  <th>夏普</th>
                  <th>最大回撤</th>
                  <th>胜率</th>
                  <th>盈亏比</th>
                  <th>交易数</th>
                  <th>网格点</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(it, i) in allRanking" :key="it.strategy">
                  <td class="num">{{ i + 1 }}</td>
                  <td class="mono-cell">{{ it.strategy_label || it.strategy }}（{{ it.strategy }}）</td>
                  <td class="mono-cell">{{ fmtAllParams(it) }}</td>
                  <td class="num">{{ fmtAllEntry(it, 'total_return') }}</td>
                  <td class="num">{{ fmtAllEntry(it, 'annual_return') }}</td>
                  <td class="num">{{ fmtAllEntry(it, 'sharpe') }}</td>
                  <td class="num">{{ fmtAllEntry(it, 'max_drawdown') }}</td>
                  <td class="num">{{ fmtAllEntry(it, 'win_rate') }}</td>
                  <td class="num">{{ fmtAllEntry(it, 'profit_factor') }}</td>
                  <td class="num">{{ fmtAllEntry(it, 'total_trades') }}</td>
                  <td class="num">{{ fmtAllEntry(it, 'grid_points') }}</td>
                  <td>
                    <button class="btn btn-ghost btn-sm" @click="onViewAll(it.strategy, it.params ?? {})">查看</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <div v-if="store.result" class="report-content">
        <div class="result-toolbar">
          <span class="result-title">
            {{ store.result.strategy }} · {{ code }} {{ codeName }}
            · {{ store.result.results.length }} 个网格点
          </span>
          <div class="toolbar-actions">
            <ExportButton :fetcher="exportOptimize" label="导出JSON" />
            <button
              type="button"
              class="btn btn-danger btn-sm"
              :disabled="deleting"
              title="删除该次寻优（任务记录与完整结果）"
              @click="onDeleteJob(store.job?.job_id)"
            >
              {{ deleting ? '删除中…' : '删除此寻优' }}
            </button>
          </div>
        </div>

        <section class="report-section panel">
          <div class="section-head">
            <h3>最优参数与评级</h3>
            <button
              type="button"
              class="btn btn-sm"
              :disabled="!store.result?.best || copying"
              title="复制最优参数 JSON，便于粘贴到回测表单"
              @click="copyBestParams"
            >
              {{ copying ? '复制中…' : '复制最优参数' }}
            </button>
          </div>
          <div class="best-block">
            <div class="best-params">
              <div class="best-label">最优参数</div>
              <div class="best-value">{{ store.result.best ? paramText(store.result.best.params) : '-' }}</div>
            </div>
            <div class="best-grade">
              <div class="best-label">寻优评级</div>
              <GradeBadge :result="bestGrade" size="lg" />
            </div>
          </div>
          <GradeDetails v-if="bestGrade" :result="bestGrade" />
          <div class="metric-cards">
            <div v-for="m in bestMetricCards" :key="m.key" class="metric-card">
              <div class="metric-label">{{ m.label }}</div>
              <div class="metric-value">{{ m.fmt(m.value) }}</div>
            </div>
          </div>
        </section>

        <section v-if="store.result.heatmap" class="report-section panel">
          <h3>参数热力图（总收益）</h3>
          <EChart :option="heatmapOption" height="380px" />
        </section>

        <section v-else-if="(store.result.param_names ?? []).length === 1" class="report-section panel">
          <h3>单参数寻优（总收益随 {{ store.result.param_names[0] }} 变化）</h3>
          <EChart :option="singleParamOption" height="320px" />
        </section>

        <section class="report-section panel">
          <h3>寻优排名（按总收益降序，前 {{ rankedResults.length }}）</h3>
          <div class="table-wrap">
            <table class="tbl">
              <thead>
                <tr>
                  <th>#</th>
                  <th>参数</th>
                  <th>总收益</th>
                  <th>夏普</th>
                  <th>最大回撤</th>
                  <th>胜率</th>
                  <th>利润因子</th>
                  <th>成交</th>
                  <th>评级</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in rankedResults" :key="i">
                  <td class="num">{{ i + 1 }}</td>
                  <td class="mono-cell">{{ paramText(r.params) }}</td>
                  <td class="num">{{ fmtSignedPct(r.total_return) }}</td>
                  <td class="num">{{ fmtNumber(r.sharpe) }}</td>
                  <td class="num">{{ fmtPct(r.max_drawdown) }}</td>
                  <td class="num">{{ fmtPct(r.win_rate) }}</td>
                  <td class="num">{{ fmtNumber(r.profit_factor) }}</td>
                  <td class="num">{{ fmtInt(r.total_trades) }}</td>
                  <td><GradeBadge :result="gradeGridPoint(r)" size="sm" :show-score="false" /></td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>

<style scoped>
.optimize-page {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.config-panel {
  width: 360px;
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
.strategy-select {
  width: 100%;
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
.result-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.result-title {
  font-size: 14px;
  font-weight: 600;
}
.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.btn-danger {
  background: var(--red-weak);
  border: 1px solid var(--red);
  color: var(--red);
}
.btn-danger:hover:not(:disabled) {
  background: var(--red);
  color: #fff;
}
.btn-sm {
  padding: 5px 12px;
  font-size: 12px;
  border-radius: var(--radius);
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.section-head h3 {
  margin-bottom: 0;
}
.report-section {
  padding: 14px 16px;
  margin-bottom: 12px;
}
.report-section h3 {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 12px;
}
.best-block {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.best-label {
  font-size: 12px;
  color: var(--text-secondary);
}
.best-value {
  font-family: var(--mono);
  font-size: 14px;
  font-weight: 600;
}
.best-grade {
  text-align: right;
}
.mono-cell {
  font-family: var(--mono);
}
.metric-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.metric-card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
}
.metric-label {
  font-size: 12px;
  color: var(--text-secondary);
}
.metric-value {
  font-family: var(--mono);
  font-size: 16px;
  font-weight: 600;
  margin-top: 2px;
}
.meta-line {
  color: var(--text-faint);
  font-size: 12px;
  margin-top: 10px;
}
.rank-section {
  margin-top: 6px;
  border-top: 1px solid var(--border);
  padding-top: 16px;
}
.workers-tip {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-faint);
}
/* 「一键寻优所有策略」按钮：暖橙渐变，比 primary 蓝更醒目 */
.run-all-btn {
  width: 100%;
  background: linear-gradient(135deg, #f59e0b 0%, #ea580c 100%);
  border: 1px solid #f59e0b;
  color: #fff;
  font-weight: 600;
  box-shadow: 0 4px 14px rgba(245, 158, 11, 0.35);
  margin-top: 10px;
}
.run-all-btn:hover:not(:disabled) {
  background: linear-gradient(135deg, #fbbf24, #f59e0b);
  border-color: #fbbf24;
  box-shadow: 0 6px 18px rgba(245, 158, 11, 0.5);
  transform: translateY(-1px);
}
.run-all-btn:active:not(:disabled) {
  transform: translateY(0);
  box-shadow: 0 2px 8px rgba(245, 158, 11, 0.35);
}
.run-all-btn:disabled {
  background: linear-gradient(135deg, #b45309, #9a3412);
  border-color: #b45309;
  color: #fde68a;
  opacity: 0.9;
  cursor: not-allowed;
  box-shadow: none;
  transform: none;
}
</style>
