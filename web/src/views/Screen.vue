<script setup lang="ts">
/** 选股结果页 /screen（契约 §5.4）
 * 条件配置（模型/池/过滤/TopN）→ 选股运行（Job 轮询）→ 结果表（分页 + 排序）→ 结果详情抽屉
 * → [导出JSON] / [导出CSV]（走契约 S6 导出端点）；历史批次回看。
 */
import { computed, onMounted, ref } from 'vue';
import { useScreenStore } from '../stores/screen';
import { useAppStore } from '../stores/app';
import type { ExportPayload } from '../api/types';
import type { FilterInfo, ScreenFilterInput, ScreenResultView, ScreenFactorRunRequest } from '../types';
import type { SecurityHit } from '../types/data';
import { fmtDateTime, fmtNumber } from '../utils/format';
import { toastError, toastSuccess, toastWarning } from '../utils/toast';
import StateBlock from '../components/StateBlock.vue';
import Pagination from '../components/Pagination.vue';
import JobProgress from '../components/JobProgress.vue';
import ExportButton from '../components/ExportButton.vue';
import SecuritySearchBox from '../components/SecuritySearchBox.vue';

const screen = useScreenStore();
const app = useAppStore();

/* ---------- 运行表单 ---------- */
const modelName = ref('');
const pool = ref<'all' | 'watchlist' | 'custom'>('all');
const customCodes = ref<string[]>([]);

function onCustomSearchSelect(hit: SecurityHit): void {
  if (!customCodes.value.includes(hit.code)) customCodes.value.push(hit.code);
}

function removeCustomCode(code: string): void {
  const idx = customCodes.value.indexOf(code);
  if (idx >= 0) customCodes.value.splice(idx, 1);
}
const topN = ref(20);
const excludeSt = ref(true);
const excludeSusp = ref(true);
const excludeNew = ref(true);
const combineMode = ref<'and' | 'or'>('and');
const asOf = ref('');

/* ---------- 单因子筛选（基于最新数据，非回测） ---------- */
const factorName = ref('');
const factorOrder = ref<'desc' | 'asc'>('desc');
const daysBack = ref<number | null>(null);
const factorAsOf = ref('');

function setLatest(): void {
  daysBack.value = null;
  factorAsOf.value = '';
}

function setDaysBack(n: number): void {
  daysBack.value = n;
  factorAsOf.value = '';
}

interface SelectedFilter {
  info: FilterInfo;
  params: Record<string, unknown>;
}

const selectedFilters = ref<SelectedFilter[]>([]);

interface ParamField {
  key: string;
  type: string;
  enumVals?: string[];
  desc?: string;
  label: string;
}

function defaultsFromSchema(schema: Record<string, unknown>): Record<string, unknown> {
  const props = (schema as { properties?: Record<string, { default?: unknown }> }).properties;
  if (!props) return {};
  const out: Record<string, unknown> = {};
  Object.entries(props).forEach(([k, v]) => {
    if (v && typeof v === 'object' && 'default' in v) out[k] = (v as { default?: unknown }).default;
  });
  return out;
}

/** 将过滤条件的 params_schema 转成面向普通用户的表单字段（无需手写 JSON） */
function filterParamFields(info: FilterInfo): ParamField[] {
  const props = (info.params_schema as { properties?: Record<string, { type?: string; enum?: string[]; description?: string; title?: string }> } | undefined)
    ?.properties;
  if (!props) return [];
  return Object.entries(props).map(([key, spec]) => ({
    key,
    type: (spec?.type as string) ?? 'string',
    enumVals: Array.isArray(spec?.enum) ? (spec!.enum as string[]) : undefined,
    desc: typeof spec?.description === 'string' ? spec.description : undefined,
    label: (spec?.title as string) ?? fieldLabel(key),
  }));
}

function fieldLabel(key: string): string {
  const map: Record<string, string> = {
    fast: '快线周期',
    slow: '慢线周期',
    value: '阈值',
    op: '方向',
    n: '数量',
    period: '周期',
    days: '天数',
    lookback: '回看天数',
    threshold: '阈值',
    percent: '比例(%)',
    window: '窗口',
    ma_type: '均线类型',
    base: '比较基准',
    indicator: '指标',
    price: '价格',
    vol: '成交量',
    amount: '成交额',
    high: '最高价',
    low: '最低价',
    close: '收盘价',
    change: '涨跌幅',
    ratio: '比率',
    count: '数量',
    min: '最小',
    max: '最大',
  };
  return map[key] ?? key;
}

function enumLabel(fieldKey: string, val: string): string {
  const map: Record<string, Record<string, string>> = {
    op: { above: '高于 / 突破', below: '低于 / 跌破' },
    indicator: { ma: 'MA 均线', macd: 'MACD', rsi: 'RSI' },
    base: { cost: '成本价', peak: '区间最高价' },
    direction: { BUY: '买入', SELL: '卖出' },
  };
  return map[fieldKey]?.[val] ?? val;
}

function isFilterSelected(info: FilterInfo): boolean {
  return selectedFilters.value.some((s) => s.info.type === info.type && s.info.condition === info.condition);
}

function toggleFilter(info: FilterInfo): void {
  const idx = selectedFilters.value.findIndex((s) => s.info.type === info.type && s.info.condition === info.condition);
  if (idx >= 0) {
    selectedFilters.value.splice(idx, 1);
  } else {
    selectedFilters.value.push({
      info,
      params: defaultsFromSchema(info.params_schema),
    });
  }
}

async function doRun(): Promise<void> {
  const filters: ScreenFilterInput[] = [];
  for (const s of selectedFilters.value) {
    filters.push({ type: s.info.type, condition: s.info.condition, params: s.params });
  }
  try {
    if (screen.mode === 'factor') {
      if (!factorName.value) {
        toastWarning('请选择单因子');
        return;
      }
      const req: ScreenFactorRunRequest = {
        factor: factorName.value,
        market: 'CN',
        pool: pool.value === 'custom' ? [...customCodes.value] : pool.value,
        top_n: topN.value,
        order: factorOrder.value,
        as_of: factorAsOf.value || null,
        days_back: daysBack.value ?? null,
        filters,
        combine: combineMode.value,
        exclude_st: excludeSt.value,
        exclude_suspended: excludeSusp.value,
        exclude_new: excludeNew.value,
      };
      await screen.runFactor(req);
      toastSuccess('单因子筛选完成');
      return;
    }
    if (!modelName.value) {
      if (screen.models.length === 0) {
        toastWarning('暂无合成模型：请先到「因子」页合成并保存模型（factor combine --save-model）');
      } else {
        toastWarning('请选择因子模型');
      }
      return;
    }
    await screen.run({
      model: modelName.value,
      market: 'CN',
      pool: pool.value === 'custom' ? [...customCodes.value] : pool.value,
      top_n: topN.value,
      filters,
      combine: combineMode.value,
      exclude_st: excludeSt.value,
      exclude_suspended: excludeSusp.value,
      exclude_new: excludeNew.value,
      as_of: asOf.value || null,
    });
    toastSuccess('选股任务已触发');
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

/* ---------- 结果排序 ---------- */
const sortableCols = computed(() => [
  { key: 'code', label: '代码' },
  { key: 'name', label: '名称' },
  { key: 'price', label: '最新价' },
  { key: 'score', label: screen.mode === 'factor' ? '因子值' : '综合得分' },
]);

function sortArrow(key: string): string {
  if (screen.sortBy !== key) return '';
  return screen.order === 'asc' ? '↑' : '↓';
}

async function clickSort(key: string): Promise<void> {
  if (screen.mode === 'factor') return; // 单因子结果已按因子值排序，无需服务端重排
  const nextOrder = screen.sortBy === key ? (screen.order === 'asc' ? 'desc' : 'asc') : key === 'score' ? 'desc' : 'asc';
  await screen.setSort(key, nextOrder);
}

/* ---------- 批次选择 ---------- */
async function selectBatch(batchId: string): Promise<void> {
  screen.selectedBatchId = batchId;
  await screen.loadResults(batchId, { page: 1 });
}

async function goPage(p: number): Promise<void> {
  await screen.goPage(p);
}

/* ---------- 导出 ---------- */
function exportJson(): Promise<ExportPayload> {
  if (!screen.selectedBatchId) {
    toastWarning('请先选择批次');
    return Promise.reject(new Error('未选择批次'));
  }
  return screen.exportResults(screen.selectedBatchId, 'json');
}

function exportCsv(): Promise<ExportPayload> {
  if (!screen.selectedBatchId) {
    toastWarning('请先选择批次');
    return Promise.reject(new Error('未选择批次'));
  }
  return screen.exportResults(screen.selectedBatchId, 'csv');
}

/* ---------- 详情抽屉 ---------- */
const detailResult = ref<ScreenResultView | null>(null);

function openDetail(r: ScreenResultView): void {
  detailResult.value = r;
}

function closeDetail(): void {
  detailResult.value = null;
}

/* ---------- 生命周期 ---------- */
onMounted(() => {
  void screen.init();
});

/* ---------- 视图计算 ---------- */
const currentBatch = computed(() => screen.batches?.items.find((b) => b.batch_id === screen.selectedBatchId) ?? null);

const resultRows = computed<ScreenResultView[]>(() =>
  screen.mode === 'factor' ? (screen.factorResults?.items ?? []) : (screen.results?.items ?? []),
);
const resultLoading = computed(() =>
  screen.mode === 'factor' ? screen.factorResultsLoading : screen.resultsLoading,
);
const resultError = computed(() =>
  screen.mode === 'factor' ? screen.factorResultsError : screen.resultsError,
);
</script>

<template>
  <div class="page">
    <div class="screen-top">
      <!-- 条件配置 -->
      <section class="panel run-panel">
        <div class="panel-title">选股条件 <span class="panel-subtitle">仅分析用途，非交易指令（NF-21/NF-22）</span></div>
        <div class="mode-tabs">
          <button type="button" :class="['mode-tab', { active: screen.mode === 'model' }]" @click="screen.mode = 'model'">因子模型（多因子）</button>
          <button type="button" :class="['mode-tab', { active: screen.mode === 'factor' }]" @click="screen.mode = 'factor'">单因子筛选（基于最新数据 · 非回测）</button>
        </div>
        <div class="form-grid">
          <div v-if="screen.mode === 'model'" class="field">
            <label>因子模型</label>
            <select v-model="modelName" class="select">
              <option value="" disabled>选择模型</option>
              <option v-for="m in screen.models" :key="m.name" :value="m.name">{{ m.name }}（{{ m.method }}）</option>
            </select>
            <p v-if="screen.models.length === 0" class="hint hint-warn">
              暂无合成模型：请先到「因子」页合成并保存模型（factor combine --save-model）
            </p>
          </div>
          <div v-if="screen.mode === 'factor'" class="field">
            <label>单因子</label>
            <select v-model="factorName" class="select" :disabled="screen.factorsLoading">
              <option value="" disabled>选择因子</option>
              <option v-for="f in screen.factors" :key="f.name" :value="f.name">{{ f.display_name || f.name }}{{ f.category ? `（${f.category}）` : '' }}</option>
            </select>
            <p v-if="screen.factors.length === 0" class="hint hint-warn">暂无因子：请先到「因子」页计算因子</p>
          </div>
          <div v-if="screen.mode === 'factor'" class="field">
            <label>排序方向</label>
            <select v-model="factorOrder" class="select">
              <option value="desc">降序（高值优先）</option>
              <option value="asc">升序（低值优先）</option>
            </select>
          </div>
          <div class="field">
            <label>样本池</label>
            <select v-model="pool" class="select">
              <option value="all">全部</option>
              <option value="watchlist">自选清单</option>
              <option value="custom">自定义代码</option>
            </select>
          </div>
          <div v-if="pool === 'custom'" class="field field-wide">
            <label>自定义标的（搜索代码/名称添加）</label>
            <SecuritySearchBox :market="'CN'" placeholder="如 600000 / 浦发" @select="onCustomSearchSelect" />
            <div v-if="customCodes.length" class="code-chips">
              <span v-for="c in customCodes" :key="c" class="code-chip">
                {{ c }}
                <button type="button" class="chip-x" @click="removeCustomCode(c)">×</button>
              </span>
            </div>
          </div>
          <div class="field">
            <label>Top N</label>
            <input v-model.number="topN" class="input" type="number" min="1" max="500" />
          </div>
          <div class="field">
            <label>{{ screen.mode === 'factor' ? '近期窗口（一键选近期数据）' : '数据基准日（留空=最新交易日）' }}</label>
            <input v-if="screen.mode === 'model'" v-model="asOf" class="input" type="date" />
            <template v-else>
              <div class="date-shortcuts">
                <button type="button" :class="['ds-btn', { active: daysBack === null && !factorAsOf }]" @click="setLatest">最新</button>
                <button type="button" :class="['ds-btn', { active: daysBack === 5 }]" @click="setDaysBack(5)">近5日</button>
                <button type="button" :class="['ds-btn', { active: daysBack === 20 }]" @click="setDaysBack(20)">近20日</button>
                <button type="button" :class="['ds-btn', { active: daysBack === 60 }]" @click="setDaysBack(60)">近60日</button>
                <button type="button" :class="['ds-btn', { active: daysBack === 120 }]" @click="setDaysBack(120)">近120日</button>
                <input v-model="factorAsOf" class="input" type="date" placeholder="自定义日期" />
              </div>
              <p class="hint">一键选择近期数据窗口，按因子最新截面快速筛选符合条件的股票</p>
            </template>
          </div>
        </div>

        <div class="form-row-inline">
          <label class="checkbox-label"><input v-model="excludeSt" type="checkbox" /> 剔除 ST</label>
          <label class="checkbox-label"><input v-model="excludeSusp" type="checkbox" /> 剔除停牌</label>
          <label class="checkbox-label"><input v-model="excludeNew" type="checkbox" /> 剔除次新</label>
          <label class="checkbox-label">
            条件组合
            <select v-model="combineMode" class="select select-inline">
              <option value="and">and</option>
              <option value="or">or</option>
            </select>
          </label>
        </div>

        <div class="filter-block">
          <div class="panel-subtitle filter-label">过滤条件（S1 插件清单）</div>
          <StateBlock v-if="screen.filtersLoading && screen.filters.length === 0" state="loading" message="加载过滤条件…" />
          <StateBlock
            v-else-if="screen.filtersError && !app.dataLakeEmpty"
            state="error"
            :message="screen.filtersError"
          />
          <StateBlock
            v-else-if="screen.filtersError && app.dataLakeEmpty"
            state="empty"
            message="数据湖为空：请先同步行情数据（见顶部引导条），再加载过滤条件"
          />
          <div v-else class="filter-grid">
            <div v-for="f in screen.filters" :key="`${f.type}-${f.condition}`" class="filter-card">
              <label class="checkbox-label">
                <input type="checkbox" :checked="isFilterSelected(f)" @change="toggleFilter(f)" />
                <b>{{ f.display_name }}</b>
                <span class="filter-tag">{{ f.type }} / {{ f.condition }}</span>
              </label>
              <p class="filter-desc">{{ f.description }}</p>
              <div v-if="isFilterSelected(f)" class="filter-form">
                <p v-if="filterParamFields(f).length === 0" class="filter-form-hint">该条件无需额外参数。</p>
                <div v-for="pf in filterParamFields(f)" :key="pf.key" class="filter-form-row">
                  <label class="filter-form-label">{{ pf.label }}</label>
                  <select
                    v-if="pf.enumVals"
                    v-model="selectedFilters.find((s) => s.info.type === f.type && s.info.condition === f.condition)!.params[pf.key]"
                    class="input select-inline"
                  >
                    <option v-for="opt in pf.enumVals" :key="opt" :value="opt">{{ enumLabel(pf.key, opt) }}</option>
                  </select>
                  <input
                    v-else-if="pf.type === 'boolean'"
                    type="checkbox"
                    v-model="selectedFilters.find((s) => s.info.type === f.type && s.info.condition === f.condition)!.params[pf.key]"
                  />
                  <input
                    v-else-if="pf.type === 'integer' || pf.type === 'number'"
                    type="number"
                    class="input"
                    v-model.number="selectedFilters.find((s) => s.info.type === f.type && s.info.condition === f.condition)!.params[pf.key]"
                  />
                  <input
                    v-else
                    type="text"
                    class="input"
                    v-model="selectedFilters.find((s) => s.info.type === f.type && s.info.condition === f.condition)!.params[pf.key]"
                  />
                  <span v-if="pf.desc" class="filter-form-hint">{{ pf.desc }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="run-actions">
          <button class="btn btn-primary" :disabled="screen.runPolling || screen.factorResultsLoading" @click="doRun">
            {{ screen.runPolling || screen.factorResultsLoading ? '执行中…' : '立即执行' }}
          </button>
        </div>

        <JobProgress v-if="screen.runJob" :job="screen.runJob" :cancelable="false" />
      </section>

      <!-- 历史批次 -->
      <section class="panel batches-panel">
        <div class="panel-title">历史批次</div>
        <StateBlock v-if="screen.batchesLoading && !screen.batches" state="loading" />
        <StateBlock v-else-if="!screen.batches || screen.batches.items.length === 0" state="empty" message="暂无批次" />
        <div v-else class="batch-list">
          <div
            v-for="b in screen.batches.items"
            :key="b.batch_id"
            class="batch-item"
            :class="{ active: screen.selectedBatchId === b.batch_id }"
            @click="selectBatch(b.batch_id)"
          >
            <div class="batch-head">
              <span class="batch-id mono-cell">{{ b.batch_id }}</span>
              <span class="badge" :class="b.status === 'done' ? 'badge-success' : b.status === 'running' ? 'badge-running' : 'badge-failed'">{{ b.status }}</span>
            </div>
            <div class="batch-meta">
              模型 {{ b.model }} · Top{{ b.top_n }} · {{ b.as_of }} · 命中 {{ b.result_count }}
            </div>
            <div class="batch-meta">耗时 {{ (b.elapsed_ms / 1000).toFixed(1) }}s · {{ fmtDateTime(b.created_at) }}</div>
          </div>
        </div>
      </section>
    </div>

    <!-- 结果表 -->
    <section class="panel results-panel">
      <div class="panel-title">
        选股结果
        <span v-if="screen.mode === 'factor' && screen.factorResults" class="panel-subtitle">
          单因子 {{ factorName }} · 数据基准日 {{ screen.factorResults.items[0]?.as_of }} · 命中 {{ screen.factorResults.total }}
        </span>
        <span v-else-if="currentBatch" class="panel-subtitle">
          {{ currentBatch.batch_id }} · 数据基准日 {{ currentBatch.as_of }} · 命中 {{ screen.results?.total ?? 0 }}
        </span>
        <span v-else class="panel-subtitle">选择批次或执行选股查看结果</span>
        <span v-if="screen.mode === 'model' && screen.selectedBatchId" class="result-actions">
          <ExportButton :fetcher="exportJson" label="导出JSON" />
          <ExportButton :fetcher="exportCsv" label="导出CSV" />
        </span>
      </div>

      <StateBlock v-if="resultLoading" state="loading" message="加载结果…" />
      <StateBlock v-else-if="resultError" state="error" :message="resultError" />
      <StateBlock v-else-if="app.dataLakeEmpty" state="empty" message="数据湖为空：请先同步行情数据（见顶部引导条），再执行选股" />
      <StateBlock v-else-if="resultRows.length === 0" state="empty" :message="screen.mode === 'factor' ? '执行单因子筛选后在此查看结果' : '请选择批次或执行选股'" />
      <template v-else>
        <div class="table-wrap">
          <table class="tbl">
            <thead>
              <tr>
                <th class="num">排名</th>
                <th
                  v-for="c in sortableCols"
                  :key="c.key"
                  class="num sortable"
                  @click="clickSort(c.key)"
                >
                  {{ c.label }} <span class="sort-arrow">{{ sortArrow(c.key) }}</span>
                </th>
                <th>触发条件</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in resultRows" :key="r.code" class="result-row" @click="openDetail(r)">
                <td class="num">{{ r.rank }}</td>
                <td class="num mono-cell">{{ r.code }}</td>
                <td>{{ r.name }}</td>
                <td class="num">{{ fmtNumber(r.price) }}</td>
                <td class="num score-cell">{{ fmtNumber(r.score, 1) }}</td>
                <td class="condition-cell" :title="r.conditions">{{ r.conditions }}</td>
                <td><button class="btn btn-ghost btn-xs" @click.stop="openDetail(r)">详情</button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <Pagination
          v-if="screen.mode === 'model' && screen.results"
          :page="screen.results.page"
          :page-size="screen.results.page_size"
          :total="screen.results.total"
          :total-pages="screen.results.total_pages"
          @change="goPage"
        />
      </template>
    </section>

    <!-- 详情抽屉 -->
    <div v-if="detailResult" class="drawer-mask" @click.self="closeDetail">
      <div class="drawer">
        <div class="drawer-title">
          <h3>{{ detailResult.name }}（{{ detailResult.code }}）</h3>
          <button class="btn btn-ghost btn-sm" @click="closeDetail">关闭</button>
        </div>
        <div class="detail-grid">
          <div class="detail-item"><span>排名</span><b>#{{ detailResult.rank }}</b></div>
          <div class="detail-item"><span>综合得分</span><b>{{ fmtNumber(detailResult.score, 1) }}</b></div>
          <div class="detail-item"><span>最新价</span><b>{{ fmtNumber(detailResult.price) }}</b></div>
          <div class="detail-item"><span>数据基准日</span><b>{{ detailResult.as_of }}</b></div>
          <div class="detail-item"><span>市场</span><b>{{ detailResult.market }}</b></div>
          <div class="detail-item"><span>触发条件</span><b>{{ detailResult.conditions }}</b></div>
        </div>
        <div class="panel-subtitle" style="margin: 12px 0 6px;">分项得分（因子 → 得分）</div>
        <table class="tbl">
          <thead>
            <tr>
              <th>因子</th>
              <th class="num">得分</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(v, k) in detailResult.sub_scores" :key="k">
              <td class="mono-cell">{{ k }}</td>
              <td class="num">{{ fmtNumber(v, 1) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.screen-top {
  display: grid;
  grid-template-columns: 3fr 2fr;
  gap: 16px;
  align-items: start;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}

.mode-tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.mode-tab {
  border: 1px solid var(--border);
  background: #fff;
  color: var(--text-secondary);
  border-radius: 6px;
  padding: 6px 14px;
  font-size: 13px;
  cursor: pointer;
}

.mode-tab:hover {
  border-color: var(--primary);
}

.mode-tab.active {
  background: var(--primary);
  border-color: var(--primary);
  color: #fff;
}

.date-shortcuts {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}

.ds-btn {
  border: 1px solid var(--border);
  background: #fff;
  color: var(--text-secondary);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
}

.ds-btn:hover {
  border-color: var(--primary);
}

.ds-btn.active {
  background: var(--primary-weak);
  border-color: var(--primary);
  color: var(--primary);
  font-weight: 600;
}

.field-wide {
  grid-column: 1 / -1;
}

.code-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
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

.form-row-inline {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin: 10px 0;
}

.select-inline {
  margin-left: 4px;
  width: auto;
  padding: 2px 8px;
}

.filter-block {
  margin: 6px 0 10px;
}

.filter-label {
  margin-bottom: 6px;
}

.filter-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}

.filter-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
}

.filter-tag {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--text-faint);
  margin-left: 6px;
}

.filter-desc {
  margin: 4px 0 0;
  font-size: 11px;
  color: var(--text-secondary);
}

.filter-form {
  margin-top: 8px;
  border-top: 1px dashed var(--border);
  padding-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.filter-form-row {
  display: grid;
  grid-template-columns: 92px 1fr;
  align-items: center;
  gap: 8px;
}

.filter-form-label {
  font-size: 12px;
  color: var(--text-secondary);
}

.filter-form-hint {
  grid-column: 1 / -1;
  font-size: 11px;
  color: var(--text-faint);
  margin: 0;
}

.run-actions {
  margin-top: 10px;
}

.batch-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 480px;
  overflow-y: auto;
}

.batch-item {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  cursor: pointer;
  font-size: 12px;
}

.batch-item:hover {
  background: #f8fafc;
}

.batch-item.active {
  border-color: var(--primary);
  background: var(--primary-weak);
}

.batch-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.batch-id {
  font-size: 12px;
}

.batch-meta {
  color: var(--text-secondary);
  font-size: 11px;
  margin-top: 2px;
}

.results-panel {
  min-width: 0;
}

.result-actions {
  display: flex;
  gap: 6px;
}

.score-cell {
  font-weight: 700;
}

.condition-cell {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.result-row {
  cursor: pointer;
}

.result-row:hover {
  background: #f8fafc;
}

.mono-cell {
  font-family: var(--mono);
  font-size: 12px;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}

.detail-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
}

.detail-item span {
  font-size: 11px;
  color: var(--text-secondary);
}

.detail-item b {
  font-size: 14px;
}

@media (max-width: 1100px) {
  .screen-top {
    grid-template-columns: 1fr;
  }

  .form-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .filter-grid {
    grid-template-columns: 1fr;
  }
}
</style>
