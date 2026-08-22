<script setup lang="ts">
/** 因子分析页 /factors（契约 §5.2）
 * 因子库列表 → 因子报告（IC 曲线/分层收益，ECharts）→ 因子计算（Job 轮询）→ 因子合成 → 模型列表
 * 各区块 [导出JSON] 直接下载对应 GET 信封响应。
 */
import { computed, onMounted, ref } from 'vue';
import type { EChartsCoreOption } from 'echarts/core';
import { useFactorStore } from '../stores/factor';
import { useAppStore } from '../stores/app';
import { api } from '../api';
import type { ExportPayload } from '../api/types';
import type { FactorInfo } from '../types';
import { envelopeToBlob } from '../utils/download';
import { fmtNumber, fmtPct, fmtDateTime } from '../utils/format';
import { toastSuccess, toastWarning } from '../utils/toast';
import EChart from '../components/EChart.vue';
import StateBlock from '../components/StateBlock.vue';
import JobProgress from '../components/JobProgress.vue';
import ExportButton from '../components/ExportButton.vue';

const factorStore = useFactorStore();
const app = useAppStore();

/* ---------- 列表过滤 ---------- */
const search = ref('');
const category = ref('全部');

const categories = computed<string[]>(() => {
  const set = new Set(factorStore.factors.map((f) => f.category));
  return ['全部', ...set];
});

const filteredFactors = computed<FactorInfo[]>(() => {
  const kw = search.value.trim().toLowerCase();
  return factorStore.factors.filter((f) => {
    if (category.value !== '全部' && f.category !== category.value) return false;
    if (!kw) return true;
    return (
      f.name.toLowerCase().includes(kw) ||
      (f.display_name ?? '').toLowerCase().includes(kw) ||
      f.description.toLowerCase().includes(kw)
    );
  });
});

/* ---------- 计算表单 ---------- */
const computeModal = ref(false);
const computeFactors = ref<string[]>([]);
const computeStart = ref('');
const computeEnd = ref('');
const computePool = ref('all');

function toggleComputeFactor(name: string): void {
  const idx = computeFactors.value.indexOf(name);
  if (idx >= 0) computeFactors.value.splice(idx, 1);
  else computeFactors.value.push(name);
}

async function computeOne(f: FactorInfo): Promise<void> {
  computeFactors.value = [f.name];
  computeStart.value = '';
  computeEnd.value = '';
  computePool.value = 'all';
  await doCompute();
}

async function doCompute(): Promise<void> {
  if (computeFactors.value.length === 0) {
    toastWarning('请选择至少一个因子');
    return;
  }
  try {
    await factorStore.compute({
      factors: [...computeFactors.value],
      market: 'CN',
      start: computeStart.value || undefined,
      end: computeEnd.value || undefined,
      pool: computePool.value,
    });
    computeModal.value = false;
    toastSuccess('因子计算任务已触发');
  } catch {
    // 错误 toast 由 api 层统一弹出
  }
}

async function cancelCompute(): Promise<void> {
  factorStore.stopComputePolling();
  factorStore.computeJob = null;
}

/* ---------- 合成表单 ---------- */
const combineFactors = ref<string[]>([]);
const combineMethod = ref<'equal' | 'ic' | 'ir'>('ir');
const combineModelName = ref('');
const combineSave = ref(true);

function toggleCombineFactor(name: string): void {
  const idx = combineFactors.value.indexOf(name);
  if (idx >= 0) combineFactors.value.splice(idx, 1);
  else combineFactors.value.push(name);
}

async function doCombine(): Promise<void> {
  if (combineFactors.value.length < 2) {
    toastWarning('请选择至少两个已计算因子');
    return;
  }
  try {
    // F5 已改为后台异步 Job（子进程隔离），点击后立即返回，不再阻塞。
    await factorStore.combine({
      factors: [...combineFactors.value],
      method: combineMethod.value,
      save_model: combineSave.value,
      model_name: combineModelName.value.trim() || undefined,
      market: 'CN',
    });
  } catch {
    // 错误 toast 由 api 层统一弹出
  }
}

/* ---------- ECharts ---------- */
const icOption = computed<EChartsCoreOption>(() => {
  const rep = factorStore.report;
  if (!rep) return {};
  const dates = rep.ic_series.map((p) => p.date);
  const ics = rep.ic_series.map((p) => p.ic);
  let cum = 0;
  const cumIc = ics.map((v) => {
    cum += v;
    return Math.round(cum * 1e6) / 1e6;
  });
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['日IC', '累计IC'], top: 0 },
    grid: { left: 52, right: 16, top: 36, bottom: 56 },
    xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', name: 'IC' },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
    series: [
      { name: '日IC', type: 'bar', data: ics, itemStyle: { color: '#2563eb' } },
      {
        name: '累计IC',
        type: 'line',
        data: cumIc,
        smooth: true,
        showSymbol: false,
        lineStyle: { color: '#d97706', width: 2 },
      },
    ],
  };
});

const quantileOption = computed<EChartsCoreOption>(() => {
  const rep = factorStore.report;
  if (!rep) return {};
  const data = rep.quantile_returns.map((v, i) => Math.round(v * 10000) / 100);
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 52, right: 16, top: 28, bottom: 30 },
    xAxis: { type: 'category', data: rep.quantile_returns.map((_, i) => `Q${i + 1}`) },
    yAxis: { type: 'value', name: '收益%' },
    series: [
      {
        type: 'bar',
        data,
        label: { show: true, position: 'top', formatter: '{c}%', fontSize: 10 },
        itemStyle: { color: '#16a34a' },
      },
    ],
  };
});

/* ---------- 导出 ---------- */
async function exportFactors(): Promise<ExportPayload> {
  const env = await api.getFactors('CN', 1, 500);
  return { blob: envelopeToBlob(env), filename: 'factor_list.json' };
}

async function exportReport(): Promise<ExportPayload> {
  const name = factorStore.selectedName;
  if (!name) throw new Error('未选择因子');
  // F4 已改为后台异步 Job：先提交，再轮询至完成，导出 result_summary。
  const env = await api.postFactorReport({ name, market: 'CN' });
  let job = env.data;
  for (let i = 0; i < 600; i++) {
    if (job.status !== 'queued' && job.status !== 'running') break;
    await new Promise((r) => setTimeout(r, 1200));
    job = (await api.getFactorJob(job.job_id)).data;
  }
  if (job.status !== 'done' || !job.result_summary) {
    throw new Error(job.error?.message ?? '报告生成失败，无法导出');
  }
  return { blob: envelopeToBlob({ data: job.result_summary }), filename: `factor_report_${name}.json` };
}

async function exportModels(): Promise<ExportPayload> {
  const env = await api.getFactorModels('CN', 1, 100);
  return { blob: envelopeToBlob(env), filename: 'factor_models.json' };
}

function factorLabel(name: string): string {
  const f = factorStore.factors.find((x) => x.name === name);
  return (f && f.display_name) || name;
}

onMounted(() => {
  void factorStore.loadFactors();
  void factorStore.loadModels();
});
</script>

<template>
  <div class="page">
    <div class="factors-layout">
      <!-- 因子库 -->
      <aside class="panel factors-sidebar">
        <div class="panel-title">
          因子库
          <span class="panel-subtitle">{{ factorStore.factorsTotal }} 个</span>
          <ExportButton :fetcher="exportFactors" label="导出JSON" />
        </div>
        <input v-model="search" class="input factor-search" type="text" placeholder="搜索因子名/描述…" />
        <div class="chip-row factor-categories">
          <button
            v-for="c in categories"
            :key="c"
            class="chip"
            :class="{ active: category === c }"
            @click="category = c"
          >
            {{ c }}
          </button>
        </div>
        <StateBlock v-if="factorStore.factorsLoading && factorStore.factors.length === 0" state="loading" />
        <StateBlock v-else-if="factorStore.factorsError && !app.dataLakeEmpty" state="error" :message="factorStore.factorsError" />
        <StateBlock v-else-if="factorStore.factorsError && app.dataLakeEmpty" state="empty" message="数据湖为空：请先同步行情数据（见顶部引导条），再刷新因子库" />
        <div v-else class="factor-list">
          <div
            v-for="f in filteredFactors"
            :key="f.name"
            class="factor-item"
            :class="{ active: factorStore.selectedName === f.name }"
            @click="factorStore.selectFactor(f.name)"
          >
            <span class="status-dot" :class="`dot-${f.status}`" :title="`状态: ${f.status}`"></span>
            <span class="factor-name">{{ f.display_name || f.name }}</span>
            <span class="factor-meta">{{ f.category }} · {{ f.source }}</span>
            <span v-if="f.display_name" class="factor-display">{{ f.name }}</span>
            <button class="btn btn-ghost btn-xs" @click.stop="computeOne(f)">计算</button>
          </div>
          <StateBlock v-if="filteredFactors.length === 0" state="empty" message="无匹配因子" />
        </div>
      </aside>

      <!-- 报告区 -->
      <section class="factors-main">
        <JobProgress
          v-if="factorStore.computeJob"
          :job="factorStore.computeJob"
          :cancelable="factorStore.computeJob.status === 'queued' || factorStore.computeJob.status === 'running'"
          @cancel="cancelCompute"
        />

        <template v-if="factorStore.report">
          <div class="panel">
            <div class="report-header">
              <div>
                <h3 class="report-title">{{ factorLabel(factorStore.report.factor) }}</h3>
                <span class="panel-subtitle">
                  区间 {{ factorStore.report.start_date }} ~ {{ factorStore.report.end_date }}
                  · 样本 {{ factorStore.report.sample_count.toLocaleString('zh-CN') }}
                  · 隔离排除 {{ factorStore.report.excluded_count }}（NF-27）
                </span>
              </div>
              <ExportButton :fetcher="exportReport" label="导出JSON" />
            </div>

            <div class="metric-cards">
              <div class="metric-card">
                <div class="metric-label">IC 均值</div>
                <div class="metric-value">{{ fmtPct(factorStore.report.ic_mean) }}</div>
                <div class="metric-sub">std {{ fmtNumber(factorStore.report.ic_std, 3) }}</div>
              </div>
              <div class="metric-card">
                <div class="metric-label">IR</div>
                <div class="metric-value">{{ fmtNumber(factorStore.report.ir) }}</div>
                <div class="metric-sub">ic_mean / ic_std</div>
              </div>
              <div class="metric-card">
                <div class="metric-label">IC 胜率</div>
                <div class="metric-value">{{ fmtPct(factorStore.report.ic_positive_rate) }}</div>
                <div class="metric-sub">正向 IC 占比</div>
              </div>
              <div class="metric-card">
                <div class="metric-label">分层换手率</div>
                <div class="metric-value">{{ fmtPct(factorStore.report.turnover_rate) }}</div>
                <div class="metric-sub">调仓换手</div>
              </div>
              <div class="metric-card">
                <div class="metric-label">多空收益 Q5−Q1</div>
                <div class="metric-value">{{ fmtPct(factorStore.report.top_minus_bottom) }}</div>
                <div class="metric-sub">分层多头−空头</div>
              </div>
              <div class="metric-card">
                <div class="metric-label">IC 自相关</div>
                <div class="metric-value">{{ fmtNumber(factorStore.report.autocorr) }}</div>
                <div class="metric-sub">衰减参考</div>
              </div>
            </div>

            <div class="charts-grid">
              <div class="chart-box chart-wide">
                <div class="chart-title">IC 时间序列（日 IC 柱状 + 累计 IC 曲线）</div>
                <EChart :option="icOption" height="300px" />
              </div>
              <div class="chart-box">
                <div class="chart-title">分层收益（Q1..Q5，%）</div>
                <EChart :option="quantileOption" height="300px" />
              </div>
            </div>
          </div>
        </template>
        <StateBlock v-else-if="factorStore.reportLoading" state="loading" message="加载因子报告…" />
        <StateBlock
          v-else-if="factorStore.reportNeedsCompute"
          state="empty"
          :message="`因子「${factorLabel(factorStore.selectedName ?? '')}」尚未计算，请先运行 compute 生成数据`"
        >
          <button
            v-if="factorStore.selectedFactor"
            class="btn btn-primary btn-sm"
            @click="computeOne(factorStore.selectedFactor)"
          >
            运行 compute
          </button>
        </StateBlock>
        <StateBlock v-else-if="factorStore.reportError && !app.dataLakeEmpty" state="error" :message="factorStore.reportError" />
        <StateBlock v-else-if="factorStore.reportError && app.dataLakeEmpty" state="empty" message="数据湖为空：因子报告依赖已计算数据，请先同步并执行因子计算（见顶部引导条）" />
        <StateBlock v-else state="empty" message="请选择左侧因子查看分析报告" />
      </section>
    </div>

    <!-- 底部：计算 / 合成 -->
    <div class="factors-bottom">
      <section class="panel">
        <div class="panel-title">
          因子计算
          <button class="btn btn-primary btn-sm" @click="computeModal = true">批量计算…</button>
        </div>
        <div class="chip-row">
          <button
            v-for="f in factorStore.factors"
            :key="f.name"
            class="chip"
            :class="{ active: computeFactors.includes(f.name) }"
            @click="toggleComputeFactor(f.name)"
          >
            {{ f.display_name || f.name }}
          </button>
        </div>
      </section>

      <section class="panel">
        <div class="panel-title">
          因子合成
          <span class="panel-subtitle">等权 / IC 加权 / IR 加权</span>
          <ExportButton :fetcher="exportModels" label="模型导出JSON" />
        </div>
        <div class="combine-form">
          <div class="chip-row combine-factors">
            <button
              v-for="f in factorStore.computedFactors"
              :key="f.name"
              class="chip"
            :class="{ active: combineFactors.includes(f.name) }"
            @click="toggleCombineFactor(f.name)"
          >
            {{ f.display_name || f.name }}
          </button>
          </div>
          <div class="combine-controls">
            <label class="checkbox-label">
              <input v-model="combineMethod" type="radio" value="equal" /> 等权
            </label>
            <label class="checkbox-label">
              <input v-model="combineMethod" type="radio" value="ic" /> IC 加权
            </label>
            <label class="checkbox-label">
              <input v-model="combineMethod" type="radio" value="ir" /> IR 加权
            </label>
            <input v-model="combineModelName" class="input" type="text" placeholder="模型名（可选）" />
            <label class="checkbox-label">
              <input v-model="combineSave" type="checkbox" /> 保存模型
            </label>
            <button class="btn btn-primary btn-sm" :disabled="factorStore.combineLoading || !!(factorStore.combineJob && ['queued','running'].includes(factorStore.combineJob.status))" @click="doCombine">
              {{ (factorStore.combineJob && ['queued','running'].includes(factorStore.combineJob.status)) ? '合成中…' : '合成' }}
            </button>
          </div>
        </div>

        <div v-if="factorStore.combineResult" class="combine-result">
          <span class="badge badge-success">合成结果</span>
          <span class="combine-name">{{ factorStore.combineResult.name }}</span>
          <span class="combine-method">{{ factorStore.combineResult.method }}</span>
          <span class="combine-weights">
            {{ Object.entries(factorStore.combineResult.weights).map(([k, v]) => `${k}: ${v}`).join('，') }}
          </span>
        </div>

        <div v-if="factorStore.models.length > 0" class="models-list">
          <table class="tbl">
            <thead>
              <tr>
                <th>模型名</th>
                <th>方法</th>
                <th>权重</th>
                <th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="m in factorStore.models" :key="m.name">
                <td class="mono-cell">{{ m.name }}</td>
                <td>{{ m.method }}</td>
                <td class="mono-cell">{{ JSON.stringify(m.weights) }}</td>
                <td>{{ fmtDateTime(m.created_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>

    <!-- 计算弹窗 -->
    <div v-if="computeModal" class="modal-mask" @click.self="computeModal = false">
      <div class="modal">
        <div class="drawer-title">
          <h3>因子计算</h3>
          <button class="btn btn-ghost btn-sm" @click="computeModal = false">关闭</button>
        </div>
        <div class="field">
          <label>选择因子</label>
          <div class="chip-row">
            <button
              v-for="f in factorStore.factors"
              :key="f.name"
              class="chip"
              :class="{ active: computeFactors.includes(f.name) }"
              @click="toggleComputeFactor(f.name)"
            >
              {{ f.name }}
            </button>
          </div>
        </div>
        <div class="form-grid">
          <div class="field">
            <label>开始日期（YYYY-MM-DD）</label>
            <input v-model="computeStart" class="input" type="date" />
          </div>
          <div class="field">
            <label>结束日期（YYYY-MM-DD）</label>
            <input v-model="computeEnd" class="input" type="date" />
          </div>
          <div class="field">
            <label>样本池</label>
            <select v-model="computePool" class="select">
              <option value="all">全部</option>
            </select>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn btn-ghost" @click="computeModal = false">取消</button>
          <button class="btn btn-primary" :disabled="factorStore.computePolling" @click="doCompute">
            开始计算
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.factors-layout {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 16px;
  align-items: start;
}

.factors-sidebar {
  position: sticky;
  top: 118px;
  max-height: calc(100vh - 140px);
  overflow-y: auto;
}

.factor-search {
  width: 100%;
  margin-bottom: 8px;
}

.factor-categories {
  margin-bottom: 10px;
}

.factor-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.factor-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid transparent;
}

.factor-item:hover {
  background: #f8fafc;
}

.factor-item.active {
  background: var(--primary-weak);
  border-color: #bfdbfe;
}

.factor-name {
  font-family: var(--mono);
  font-weight: 600;
  font-size: 12px;
}

.factor-meta {
  font-size: 11px;
  color: var(--text-faint);
}

.factor-display {
  font-size: 11px;
  color: var(--text-secondary);
  margin-left: auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 80px;
}

.factors-main {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.report-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.report-title {
  margin: 0;
  font-size: 18px;
  font-family: var(--mono);
}

.charts-grid {
  display: grid;
  grid-template-columns: 3fr 2fr;
  gap: 16px;
  margin-top: 14px;
}

.chart-box {
  min-width: 0;
}

.chart-title {
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.factors-bottom {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  align-items: start;
}

.combine-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.combine-factors {
  max-height: 120px;
  overflow-y: auto;
}

.combine-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.combine-controls .input {
  width: 160px;
}

.combine-result {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding: 8px 10px;
  background: var(--green-weak);
  border-radius: 6px;
  font-size: 12px;
  flex-wrap: wrap;
}

.combine-name {
  font-weight: 600;
  font-family: var(--mono);
}

.combine-method {
  color: var(--text-secondary);
}

.combine-weights {
  font-family: var(--mono);
  color: var(--text-secondary);
}

.models-list {
  margin-top: 12px;
}

.mono-cell {
  font-family: var(--mono);
  font-size: 12px;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin: 12px 0;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}

@media (max-width: 1100px) {
  .factors-layout,
  .factors-bottom {
    grid-template-columns: 1fr;
  }

  .factors-sidebar {
    position: static;
    max-height: none;
  }

  .charts-grid {
    grid-template-columns: 1fr;
  }
}
</style>
