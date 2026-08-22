<script setup lang="ts">
/**
 * 结果对比页 /compare（契约 v1.3 草案 C1，docs/06 §2.4）。
 * 左栏：C1 任务列表（action 类型/标的/策略/状态/时间）+ 状态过滤 + 分页 + 勾选 2-4 个 done 任务
 * 右栏：对比 → 归一化净值曲线叠加图 + 绩效指标对比表（行 × 任务列）+ 导出 JSON。
 * 寻优任务（O3）无净值曲线时仅参与指标表，叠加图跳过并提示。
 */
import { computed, onMounted, ref } from 'vue';
import type { EChartsCoreOption } from 'echarts/core';
import { useCompareStore, COMPARE_METRIC_KEYS } from '../stores/compare';
import type { CompareItem } from '../stores/compare';
import { fmtDateTime, fmtInt, fmtNumber, fmtPct, fmtSignedPct } from '../utils/format';
import EChart from '../components/EChart.vue';
import ExportButton from '../components/ExportButton.vue';
import GradeBadge from '../components/GradeBadge.vue';
import Pagination from '../components/Pagination.vue';
import StateBlock from '../components/StateBlock.vue';

const store = useCompareStore();

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: '全部状态' },
  { value: 'done', label: '已完成' },
  { value: 'running', label: '运行中' },
  { value: 'queued', label: '排队中' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
];

const ACTION_LABEL: Record<string, string> = {
  backtest: '回测',
  portfolio: '组合回测',
  multi: '多策略',
  optimize: '寻优',
};

function actionLabel(action: string): string {
  return ACTION_LABEL[action] ?? action;
}

function statusLabel(status: string): string {
  return (
    {
      queued: '排队中',
      running: '运行中',
      done: '已完成',
      failed: '失败',
      cancelled: '已取消',
    }[status] ?? status
  );
}

/** 任务表格行展示摘要（result_summary 尽力解析，不做业务兜底） */
function summaryText(summary: Record<string, unknown> | null): string {
  if (!summary) return '-';
  const s = summary as Record<string, unknown>;
  const strategy = typeof s.strategy === 'string' ? s.strategy : '';
  const code = typeof s.code === 'string' ? s.code : '';
  const codes = Array.isArray(s.codes) ? (s.codes as string[]).join(',') : '';
  const brief = code || codes;
  const parts: string[] = [];
  if (strategy) parts.push(strategy);
  if (brief) parts.push(brief);
  return parts.join(' · ') || '-';
}

/* ---------- 勾选 ---------- */
function isSelected(jobId: string): boolean {
  return store.selected.includes(jobId);
}

function toggle(job: { job_id: string; status: string }): void {
  if (job.status !== 'done') return;
  store.toggleSelected(job.job_id);
}

async function onCompare(): Promise<void> {
  await store.runCompare();
}

/* ---------- 叠加图（归一化净值，多线） ---------- */
const compareChartOption = computed<EChartsCoreOption>(() => {
  const items = store.items.filter((i): i is CompareItem & { equity: NonNullable<CompareItem['equity']> } => !!i.equity);
  if (items.length === 0) return {};
  const dates = new Set<string>();
  const normMap: Record<string, Map<string, number>> = {};
  for (const item of items) {
    const base = item.equity.length > 0 && item.equity[0].total !== 0 ? item.equity[0].total : 1;
    const m = new Map<string, number>();
    for (const p of item.equity) {
      m.set(p.datetime, Math.round((p.total / base) * 1e6) / 1e6);
      dates.add(p.datetime);
    }
    normMap[item.jobId] = m;
  }
  const ordered = Array.from(dates).sort();
  const series = items.map((item) => ({
    name: item.label,
    type: 'line' as const,
    showSymbol: false,
    data: ordered.map((d) => normMap[item.jobId]?.get(d) ?? null),
    lineStyle: { width: 2 },
    connectNulls: true,
  }));
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: items.map((i) => i.label), type: 'scroll' },
    grid: { left: 60, right: 24, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: ordered },
    yAxis: { type: 'value', name: '归一化净值', scale: true },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18 }],
    series,
  };
});

/** 指标对比表：行 = 指标，列 = 任务 */
const metricRows = computed(() => {
  return COMPARE_METRIC_KEYS.map((mk) => ({
    key: mk.key,
    label: mk.label,
    values: store.items.map((item) => item.metrics[mk.key] ?? null),
    fmt: metricFormatter(mk.key),
  }));
});

function metricFormatter(key: string): (v: number | null) => string {
  if (key === 'total_return' || key === 'annual_return' || key === 'max_drawdown' || key === 'win_rate' || key === 'volatility') {
    return (v) => fmtPct(v);
  }
  if (key === 'total_trades') return (v) => fmtInt(v);
  return (v) => fmtNumber(v);
}

async function exportCompare(): Promise<{ blob: Blob; filename: string }> {
  if (store.items.length === 0) throw new Error('无对比结果可导出');
  const payload = {
    exported_at: new Date().toISOString(),
    jobs: store.selected.map((id) => store.jobs.find((j) => j.job_id === id) ?? null).filter(Boolean),
    items: store.items.map((it) => ({
      job_id: it.jobId,
      action: it.action,
      label: it.label,
      grade: it.grade?.grade ?? null,
      metrics: it.metrics,
      equity: it.equity,
    })),
  };
  return {
    blob: new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json; charset=utf-8' }),
    filename: `compare_${new Date().toISOString().slice(0, 10)}.json`,
  };
}

onMounted(() => {
  void store.loadJobs();
});
</script>

<template>
  <div class="compare-page">
    <!-- 左栏：任务列表 -->
    <aside class="config-panel panel">
      <div class="panel-title">
        回测任务
        <button class="btn btn-ghost btn-xs" :disabled="store.loading" @click="store.loadJobs()">
          {{ store.loading ? '加载中…' : '刷新' }}
        </button>
      </div>

      <StateBlock v-if="store.listError" state="error" :message="store.listError" />
      <StateBlock v-else-if="store.loading && store.jobs.length === 0" state="loading" />

      <template v-else>
        <div v-if="store.jobs.length === 0" class="empty-block">
          <p>暂无回测任务</p>
          <p class="hint">请先到「选股回测 / 组合回测 / 参数寻优」运行任务，完成后回到本页勾选对比。</p>
        </div>

        <template v-else>
          <div class="filter-row">
            <select v-model="store.statusFilter" class="input filter-select" @change="store.setStatusFilter(store.statusFilter)">
              <option v-for="o in STATUS_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </div>

          <div class="task-list">
            <label
              v-for="job in store.filteredJobs"
              :key="job.job_id"
              class="task-item"
              :class="{ disabled: job.status !== 'done' }"
              :title="job.status !== 'done' ? '仅已完成任务可对比' : ''"
            >
              <input
                type="checkbox"
                :checked="isSelected(job.job_id)"
                :disabled="job.status !== 'done' || (!isSelected(job.job_id) && store.selected.length >= 4)"
                @change="toggle(job)"
              />
              <span class="task-body">
                <span class="task-line1">
                  <span class="badge" :class="`badge-${job.status}`">{{ statusLabel(job.status) }}</span>
                  <span class="task-action">{{ actionLabel(job.action) }}</span>
                </span>
                <span class="task-line2">{{ summaryText(job.result_summary) }}</span>
                <span class="task-line3">
                  {{ fmtDateTime(job.created_at) }}
                  <span class="task-id" :title="job.job_id">{{ job.job_id }}</span>
                </span>
              </span>
            </label>
          </div>

          <Pagination
            :page="store.page"
            :page-size="store.pageSize"
            :total="store.filteredTotal"
            :total-pages="store.filteredTotalPages"
            @change="(p) => (store.page = p)"
          />
        </template>
      </template>

      <button
        class="btn btn-primary compare-btn"
        :disabled="store.selected.length < 2 || store.comparing"
        @click="onCompare"
      >
        {{ store.comparing ? '对比中…' : `对比所选（${store.selected.length}）` }}
      </button>
      <p class="hint">勾选 2-4 个已完成任务后点击「对比」。</p>
    </aside>

    <!-- 右栏：对比报告 -->
    <main class="report-panel">
      <div v-if="store.compareError" class="error-banner">⚠ {{ store.compareError }}</div>

      <div v-if="store.items.length < 2" class="placeholder panel">
        <p>勾选至少 2 个已完成任务并点击「对比」，将展示归一化净值曲线叠加与指标对比表。</p>
      </div>

      <div v-else class="report-content">
        <div class="result-toolbar">
          <span class="result-title">对比 {{ store.items.length }} 个任务</span>
          <ExportButton :fetcher="exportCompare" label="导出JSON" />
        </div>

        <section class="report-section panel">
          <h3>净值曲线对比（归一化）</h3>
          <div v-if="store.items.some((i) => !!i.equity)">
            <EChart :option="compareChartOption" height="380px" />
            <p v-if="store.items.some((i) => !i.equity)" class="hint">
              ⚠ {{ store.items.filter((i) => !i.equity).length }} 个寻优任务无净值曲线，未纳入叠加图（指标表仍包含其最优绩效）。
            </p>
          </div>
          <div v-else class="hint">所选任务均无净值曲线（寻优结果），仅展示指标对比。</div>
        </section>

        <section class="report-section panel">
          <h3>绩效指标对比</h3>
          <div class="table-wrap">
            <table class="tbl compare-tbl">
              <thead>
                <tr>
                  <th>指标</th>
                  <th v-for="item in store.items" :key="item.jobId" class="col-item">
                    <span class="col-label">{{ item.label }}</span>
                    <GradeBadge :result="item.grade" size="sm" :show-score="false" />
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in metricRows" :key="row.key">
                  <td class="row-label">{{ row.label }}</td>
                  <td v-for="(v, i) in row.values" :key="i" class="num">{{ row.fmt(v) }}</td>
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
.compare-page {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.config-panel {
  width: 380px;
  flex-shrink: 0;
  padding: 14px 16px;
}
.filter-row {
  margin-bottom: 8px;
}
.filter-select {
  width: 100%;
}
.task-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 520px;
  overflow-y: auto;
  margin-bottom: 4px;
}
.task-item {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  cursor: pointer;
  font-size: 12px;
}
.task-item:hover {
  border-color: var(--primary);
}
.task-item.disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.task-item input {
  margin-top: 2px;
}
.task-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.task-line1 {
  display: flex;
  align-items: center;
  gap: 6px;
}
.task-action {
  font-weight: 600;
}
.task-line2 {
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.task-line3 {
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 11px;
  display: flex;
  justify-content: space-between;
  gap: 6px;
}
.task-id {
  overflow: hidden;
  text-overflow: ellipsis;
}
.empty-block {
  padding: 16px 4px;
  color: var(--text-secondary);
  font-size: 13px;
}
.hint {
  color: var(--text-faint);
  font-size: 12px;
  margin: 6px 0 0;
}
.compare-btn {
  width: 100%;
  margin-top: 8px;
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
.report-section {
  padding: 14px 16px;
  margin-bottom: 12px;
}
.report-section h3 {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 12px;
}
.compare-tbl th.col-item {
  min-width: 140px;
}
.col-label {
  display: block;
  margin-bottom: 4px;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-label {
  font-weight: 600;
  color: var(--text-secondary);
}
</style>
