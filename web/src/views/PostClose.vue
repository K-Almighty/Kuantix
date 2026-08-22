<script setup lang="ts">
/** 盘后复盘视图：涨跌停汇总 + 类型/行业分布饼图 + 涨跌停条目表 + 技术扫描亮点 + 自选浮盈 */
import { onMounted, ref, computed, watch } from 'vue';
import { useAnalysisStore } from '../stores/analysis';
import { api } from '../api';
import Pagination from '../components/Pagination.vue';
import ExportButton from '../components/ExportButton.vue';
import EChart from '../components/EChart.vue';
import type { LimitType } from '../types';
import type { EChartsCoreOption } from 'echarts/core';
import { fmtNumber } from '../utils/format';

/** 支撑/压力位数组统一 2 位小数显示 */
const fmtLevels = (levels: number[] | undefined): string =>
  levels && levels.length ? levels.map((x) => fmtNumber(x, 2)).join(',') : '—';

const store = useAnalysisStore();
const rerunLoading = ref(false);
const force = ref(false);

const limitSideOptions = [
  { label: '全部', value: 'all' as const },
  { label: '只看涨停', value: 'up' as const },
  { label: '只看跌停', value: 'down' as const },
];

/** 涨跌停类型：按后端 6 种枚举值 + 其他兜底，value 使用中文枚举值 */
const limitTypeOptions: { label: string; value: LimitType | '' }[] = [
  { label: '全部', value: '' },
  { label: '业绩驱动', value: '业绩驱动' },
  { label: '概念炒作', value: '概念炒作' },
  { label: '技术突破', value: '技术突破' },
  { label: '新股上市', value: '新股上市' },
  { label: 'ST摘帽', value: 'ST摘帽' },
  { label: '其他', value: '其他' },
];

const byTypeChart = computed<EChartsCoreOption | null>(() => {
  const arr = store.postReport?.limit_summary?.by_type ?? store.limit?.summary?.by_type ?? [];
  if (!arr.length) return null;
  return {
    title: { text: '涨停类型分布', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [{ type: 'pie', radius: ['40%', '70%'], data: arr.map((x) => ({ name: x.limit_type as string, value: x.count })) }],
  } as EChartsCoreOption;
});

const bySectorChart = computed<EChartsCoreOption | null>(() => {
  const arr = store.postReport?.limit_summary?.by_sector ?? store.limit?.summary?.by_sector ?? [];
  if (!arr.length) return null;
  const sorted = [...arr].sort((a, b) => (b.up + b.down) - (a.up + a.down)).slice(0, 15);
  return {
    title: { text: '行业涨跌停 Top 15', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0 },
    grid: { left: 80, right: 20, top: 40, bottom: 40 },
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: sorted.map((x) => x.sector) },
    series: [
      { name: '涨停', type: 'bar', stack: 't', data: sorted.map((x) => x.up), itemStyle: { color: '#d9534f' } },
      { name: '跌停', type: 'bar', stack: 't', data: sorted.map((x) => x.down), itemStyle: { color: '#3a8b3a' } },
    ],
  } as EChartsCoreOption;
});

function changePct(pct: number) {
  return `${(pct * 100).toFixed(2)}%`;
}

async function doRerun() {
  rerunLoading.value = true;
  try {
    await store.loadPostCloseReport(true);
    await store.loadLimit(1);
    await store.loadTechnical(1);
  } finally {
    rerunLoading.value = false;
  }
}

function reloadAll() {
  store.loadPostCloseReport();
  store.loadLimit(1);
  store.loadTechnical(1);
}

watch(
  () => store.market,
  () => reloadAll(),
);

onMounted(reloadAll);
</script>

<template>
  <div class="analysis-view post-close-view">
    <section class="panel">
      <header class="panel-header">
        <div>
          <h2>盘后复盘报告</h2>
          <p class="panel-sub" v-if="store.postReport">
            生成时间 {{ store.postReport.generated_at }} · 市场 {{ store.postReport.market }} · {{ store.postReport.date }}
            <span v-if="store.postReport.data_source === 'tdx_realtime'" class="src-tag src-tdx">数据源: tdx 实时</span>
            <span v-else-if="store.postReport.data_source === 'lake_fallback'" class="src-tag src-fallback">
              数据源: 本地(截至 {{ store.postReport.data_as_of }}，未同步至当天)
            </span>
            <span v-else class="src-tag src-lake">数据源: 本地</span>
          </p>
        </div>
        <div class="panel-actions">
          <input v-model="store.market" placeholder="CN" class="input input-xs" style="width: 80px" />
          <label class="cb"><input type="checkbox" v-model="force" /> 强制跳过收盘等待</label>
          <button class="btn btn-xs" :disabled="rerunLoading" @click="doRerun">
            {{ rerunLoading ? '重算中…' : '手动重算' }}
          </button>
          <ExportButton label="导出JSON" :fetcher="() => api.exportPostCloseReport('json', { market: store.market })" />
          <ExportButton label="导出Markdown" :fetcher="() => api.exportPostCloseReport('md', { market: store.market })" />
        </div>
      </header>

      <div v-if="store.postReportLoading" class="placeholder">加载报告中…</div>
      <div v-else-if="store.postReportError" class="placeholder error">{{ store.postReportError }}</div>
      <div v-else-if="store.postReport" class="summary-stats">
        <div class="stat">
          <div class="stat-label">上涨家数</div>
          <div class="stat-val up">{{ store.postReport.limit_summary?.up_count ?? 0 }}</div>
        </div>
        <div class="stat">
          <div class="stat-label">下跌家数</div>
          <div class="stat-val down">{{ store.postReport.limit_summary?.down_count ?? 0 }}</div>
        </div>
        <div class="stat">
          <div class="stat-label">平盘</div>
          <div class="stat-val">{{ store.postReport.limit_summary?.flat_count ?? 0 }}</div>
        </div>
        <div class="stat">
          <div class="stat-label">涨跌停合计</div>
          <div class="stat-val">{{ store.postReport.limit_summary?.total_count ?? 0 }}</div>
        </div>
        <div class="stat">
          <div class="stat-label">涨停占比</div>
          <div class="stat-val up">{{ ((store.postReport.limit_summary?.up_ratio ?? 0) * 100).toFixed(1) }}%</div>
        </div>
        <div class="stat">
          <div class="stat-label">跌停占比</div>
          <div class="stat-val down">{{ ((store.postReport.limit_summary?.down_ratio ?? 0) * 100).toFixed(1) }}%</div>
        </div>
      </div>

      <div v-if="store.postReport" class="charts-row">
        <div class="card">
          <EChart v-if="byTypeChart" :option="byTypeChart" height="280px" />
          <div v-else class="placeholder">暂无类型分布数据</div>
        </div>
        <div class="card">
          <EChart v-if="bySectorChart" :option="bySectorChart" height="280px" />
          <div v-else class="placeholder">暂无行业分布数据</div>
        </div>
      </div>

      <div v-if="store.postReport" class="two-col">
        <div class="card">
          <h4>今日信号</h4>
          <table v-if="store.postReport.signals_today.length" class="tbl tbl-sm">
            <thead><tr><th>代码</th><th>名称</th><th>信号</th><th>日期</th></tr></thead>
            <tbody>
              <tr v-for="(s, i) in store.postReport.signals_today.slice(0, 50)" :key="i">
                <td>{{ s.code }}</td>
                <td>{{ s.name || '—' }}</td>
                <td class="mono">{{ (s.signals || []).join(' · ') || '—' }}</td>
                <td>{{ s.date }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="placeholder">暂无</div>
        </div>
        <div class="card">
          <h4>自选浮盈</h4>
          <table v-if="store.postReport.watchlist_pnl.length" class="tbl tbl-sm">
            <thead><tr><th>代码</th><th>名称</th><th>最新价</th><th>成本价</th><th>浮盈</th><th>比例</th></tr></thead>
            <tbody>
              <tr v-for="p in store.postReport.watchlist_pnl.slice(0, 50)" :key="p.code">
                <td>{{ p.code }}</td>
                <td>{{ p.name }}</td>
                <td class="mono">{{ Number(p.last).toFixed(2) }}</td>
                <td class="mono">{{ p.cost ? Number(p.cost).toFixed(2) : '—' }}</td>
                <td class="mono" :class="Number(p.pnl) >= 0 ? 'up' : 'down'">{{ p.pnl == null ? '—' : Number(p.pnl).toFixed(0) }}</td>
                <td class="mono" :class="Number(p.pnl_pct) >= 0 ? 'up' : 'down'">{{ p.pnl_pct == null ? '—' : (Number(p.pnl_pct) * 100).toFixed(2) + '%' }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="placeholder">暂无</div>
        </div>
      </div>
    </section>

    <!-- 涨跌停条目 -->
    <section class="panel">
      <header class="panel-header">
        <div><h2>涨跌停条目</h2><p class="panel-sub">按类型/行业/涨跌方向过滤，分页展示</p></div>
        <div class="panel-actions">
          <select v-model="store.limitSide" class="input input-xs" @change="store.loadLimit(1)">
            <option v-for="o in limitSideOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
          <select v-model="store.limitType" class="input input-xs" @change="store.loadLimit(1)">
            <option v-for="o in limitTypeOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
          <input v-model="store.limitSector" placeholder="行业过滤" class="input input-xs" style="width: 140px" />
          <button class="btn btn-xs" @click="store.loadLimit(1)">刷新</button>
        </div>
      </header>

      <div v-if="!store.limitLoading && store.limit?.summary" class="summary-mini">
        涨停 {{ store.limit.summary.up_count }} · 跌停 {{ store.limit.summary.down_count }} ·
        平盘 {{ store.limit.summary.flat_count }} · 当日共 {{ store.limit.summary.total_count }} 条
      </div>

      <div v-if="store.limitLoading" class="placeholder">加载中…</div>
      <table v-else-if="store.limit?.entries" class="tbl">
        <thead><tr>
          <th>代码</th><th>名称</th><th>方向</th><th>类型</th><th>收盘价</th>
          <th>涨跌幅</th><th>量比</th><th>连板</th><th>行业</th><th>原因</th>
        </tr></thead>
        <tbody>
          <tr v-for="e in store.limit.entries.items" :key="e.code">
            <td>{{ e.code }}</td>
            <td>{{ e.name }}</td>
            <td>
              <span :class="'dir ' + (e.is_up ? 'up' : 'down')">{{ e.is_up ? '↑ 涨停' : '↓ 跌停' }}</span>
            </td>
            <td>{{ e.limit_type }}</td>
            <td class="mono">{{ e.close.toFixed(2) }}</td>
            <td class="mono" :class="e.is_up ? 'up' : 'down'">{{ changePct(e.change_pct) }}</td>
            <td class="mono">{{ e.volume_ratio?.toFixed(2) ?? '—' }}</td>
            <td>{{ e.continuous_days }}天</td>
            <td>{{ e.sector }}</td>
            <td class="muted">{{ e.reasons.join(' · ') || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <Pagination
        v-if="store.limit?.entries && store.limit.entries.total > store.limit.entries.page_size"
        :page="store.limit.entries.page"
        :page-size="store.limit.entries.page_size"
        :total="store.limit.entries.total"
        :total-pages="store.limit.entries.total_pages"
        @change="store.loadLimit($event)"
      />
    </section>

    <!-- 技术扫描 -->
    <section class="panel">
      <header class="panel-header">
        <div><h2>技术面扫描</h2><p class="panel-sub">MACD/RSI/KDJ/BOLL + 均线 + 支撑压力位判断</p></div>
        <div class="panel-actions">
          <input v-model="store.technicalCodes" placeholder="代码集合（逗号分隔，留空=抽样）" class="input input-xs" style="width: 260px" />
          <button class="btn btn-xs" @click="store.loadTechnical(1)">扫描</button>
        </div>
      </header>
      <div v-if="store.technicalLoading" class="placeholder">加载中…</div>
      <table v-else-if="store.technical" class="tbl">
        <thead><tr>
          <th>代码</th><th>名称</th><th>MA5/10/20/60</th><th>MACD</th><th>RSI</th><th>KDJ K/D/J</th><th>BOLL 上/中/下</th>
          <th>趋势</th><th>支撑 / 压力</th><th>信号</th>
        </tr></thead>
        <tbody>
          <tr v-for="t in store.technical.items" :key="t.code">
            <td>
              {{ t.code }}
              <span v-if="t.data_source === 'tdx_realtime'" class="src-tag src-tdx" title="tdx 实时数据">实时</span>
            </td>
            <td>{{ t.name || '—' }}</td>
            <td class="mono">
              {{ t.ma5?.toFixed(2) ?? '—' }} / {{ t.ma10?.toFixed(2) ?? '—' }} /
              {{ t.ma20?.toFixed(2) ?? '—' }} / {{ t.ma60?.toFixed(2) ?? '—' }}
            </td>
            <td class="mono">
              DIF {{ t.macd_dif_last?.toFixed(3) ?? '—' }}<br />
              DEA {{ t.macd_dea_last?.toFixed(3) ?? '—' }}<br />
              HIST {{ t.macd_hist_last?.toFixed(3) ?? '—' }}
            </td>
            <td class="mono">{{ t.rsi_last?.toFixed(1) ?? '—' }}</td>
            <td class="mono">
              {{ t.kdj_k_last?.toFixed(1) ?? '—' }} / {{ t.kdj_d_last?.toFixed(1) ?? '—' }} /
              {{ t.kdj_j_last?.toFixed(1) ?? '—' }}
            </td>
            <td class="mono">
              {{ t.boll_upper_last?.toFixed(2) ?? '—' }}<br />
              {{ t.boll_mid_last?.toFixed(2) ?? '—' }}<br />
              {{ t.boll_lower_last?.toFixed(2) ?? '—' }}
            </td>
            <td>
              <span :class="'trend ' + t.trend_direction">
                {{ t.trend_direction === 'up' ? '↑' : t.trend_direction === 'down' ? '↓' : '→' }}
                {{ (t.trend_strength * 100).toFixed(0) }}%
              </span>
            </td>
            <td class="mono">{{ fmtLevels(t.support_levels) }} / {{ fmtLevels(t.resistance_levels) }}</td>
            <td class="mono">{{ t.signals.join(' · ') || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <Pagination
        v-if="store.technical && store.technical.total > store.technical.page_size"
        :page="store.technical.page"
        :page-size="store.technical.page_size"
        :total="store.technical.total"
        :total-pages="store.technical.total_pages"
        @change="store.loadTechnical($event)"
      />
    </section>
  </div>
</template>

<style scoped>
.post-close-view { display: flex; flex-direction: column; gap: 16px; }
.summary-stats {
  display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px;
  padding: 12px 4px;
}
.stat { background: #fafafa; border: 1px solid #eee; border-radius: 8px; padding: 12px; }
.stat-label { font-size: 12px; color: #888; margin-bottom: 4px; }
.stat-val { font-size: 22px; font-weight: 700; }
.stat-val.up { color: var(--up, #d9534f); }
.stat-val.down { color: var(--down, #3a8b3a); }
.charts-row { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-top: 8px; }
.two-col { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-top: 16px; }
.summary-mini { font-size: 13px; color: #555; margin: 8px 0 12px; }
.dir.up { color: var(--up, #d9534f); font-weight: 700; }
.dir.down { color: var(--down, #3a8b3a); font-weight: 700; }
.trend.up { color: var(--up, #d9534f); font-weight: 600; }
.trend.down { color: var(--down, #3a8b3a); font-weight: 600; }
.trend.flat { color: var(--muted, #888); }
.muted { color: var(--muted, #888); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.cb { display: inline-flex; align-items: center; gap: 4px; }
.up { color: var(--up, #d9534f); }
.down { color: var(--down, #3a8b3a); }
.src-tag { margin-left: 8px; font-size: 12px; padding: 1px 6px; border-radius: 4px; vertical-align: middle; }
.src-tdx { color: #2563eb; background: #eff6ff; border: 1px solid #bfdbfe; }
.src-lake { color: #16a34a; background: #f0fdf4; border: 1px solid #bbf7d0; }
.src-fallback { color: #d97706; background: #fffbeb; border: 1px solid #fde68a; }
</style>
