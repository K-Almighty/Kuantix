<script setup lang="ts">
/** 盘前分析视图：顶部报告概览 + 消息面过滤列表 + 自选基本面画像表格 */
import { onMounted, ref, computed, watch } from 'vue';
import { useAnalysisStore } from '../stores/analysis';
import { api } from '../api';
import Pagination from '../components/Pagination.vue';
import ExportButton from '../components/ExportButton.vue';
import EChart from '../components/EChart.vue';
import type { NewsCategory, FundamentalGrade } from '../types';
import type { EChartsCoreOption } from 'echarts/core';

const store = useAnalysisStore();

/**
 * GradeBadge 需要 GradeResult（综合评分 + vetoes），基本面 FundamentalGrade 只有字母等级，
 * 这里以原生 <span class="grade-badge-inline"> 形式渲染，避免强耦合 grading 模块。
 */
function gradeBadgeProps(grade: FundamentalGrade) {
  // 返回符合 GradeResult 形状的最小对象：grading 内部只需要 grade 字段即可定位 meta
  const score = grade === 'A' ? 88 : grade === 'B' ? 70 : grade === 'C' ? 55 : 35;
  return { result: { grade, score, insufficientSample: false, isLosing: false, vetoes: [], performance: {} as any, stats: {} as any } } as any;
}

const rerunLoading = ref(false);
const rerunFundamentalsInput = ref('');

/* ---- 查询面板 ---- */
const categoryOptions: { label: string; value: NewsCategory | '' }[] = [
  { label: '全部', value: '' },
  { label: '新闻', value: 'news' },
  { label: '公告', value: 'announcement' },
  { label: '政策', value: 'policy' },
];
const gradeOptions: { label: string; value: FundamentalGrade | '' }[] = [
  { label: '全部', value: '' },
  { label: 'A', value: 'A' },
  { label: 'B', value: 'B' },
  { label: 'C', value: 'C' },
  { label: 'D', value: 'D' },
];

/* ---- 图表：消息分类 ---- */
const newsCategoryChart = computed<EChartsCoreOption | null>(() => {
  const summary = store.preReport?.news_feed_summary;
  if (!summary?.by_category?.length) return null;
  const data = summary.by_category.map((c) => ({
    name: c.category === 'news' ? '新闻' : c.category === 'announcement' ? '公告' : '政策',
    value: c.count,
  }));
  return {
    title: { text: '消息分类占比', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [{ type: 'pie', radius: ['40%', '70%'], data }],
  } as EChartsCoreOption;
});

/* ---- 加载流程 ---- */
async function doRerun() {
  rerunLoading.value = true;
  try {
    await store.loadPreOpenReport(true);
  } finally {
    rerunLoading.value = false;
  }
}

function reloadAll() {
  store.loadPreOpenReport();
  store.loadNews(1);
  store.loadFundamentals(1);
}

watch(
  () => [store.market, store.date] as const,
  () => reloadAll(),
);

onMounted(reloadAll);
</script>

<template>
  <div class="analysis-view pre-open-view">
    <section class="panel">
      <header class="panel-header">
        <div>
          <h2>盘前分析报告</h2>
          <p class="panel-sub" v-if="store.preReport">
            生成时间 {{ store.preReport.generated_at }} · 市场 {{ store.preReport.market }} · {{ store.preReport.date }}
          </p>
        </div>
        <div class="panel-actions">
          <input
            v-model="store.market"
            placeholder="市场 CN"
            class="input input-xs"
            style="width: 80px"
          />
          <input
            v-model="store.date"
            type="date"
            class="input input-xs"
            style="width: 150px"
          />
          <button class="btn btn-xs" :disabled="rerunLoading" @click="doRerun">
            {{ rerunLoading ? '重算中…' : '手动重算' }}
          </button>
          <ExportButton label="导出JSON" :fetcher="() => api.exportPreOpenReport('json', { market: store.market, date: store.date || undefined })" />
          <ExportButton label="导出Markdown" :fetcher="() => api.exportPreOpenReport('md', { market: store.market, date: store.date || undefined })" />
        </div>
      </header>

      <div v-if="store.preReportLoading" class="placeholder">加载报告中…</div>
      <div v-else-if="store.preReportError" class="placeholder error">{{ store.preReportError }}</div>
      <div v-else-if="store.preReport" class="report-grid">
        <div class="card">
          <h4>消息面汇总</h4>
          <p>当日条目：<b>{{ store.preReport.news_feed_summary.total || 0 }}</b></p>
          <EChart v-if="newsCategoryChart" :option="newsCategoryChart" height="220px" />
          <div v-else class="placeholder">暂无消息面数据</div>
          <h4 v-if="store.preReport.news_feed_summary.top_news?.length" style="margin-top: 16px">Top 重要消息</h4>
          <ul class="news-list-sm">
            <li v-for="n in store.preReport.news_feed_summary.top_news.slice(0, 6)" :key="n.id">
              <a :href="n.url" target="_blank" rel="noreferrer">[{{ n.importance }}] {{ n.title }}</a>
              <div class="muted">{{ n.publish_ts }} · {{ n.source }}</div>
            </li>
          </ul>
        </div>
        <div class="card">
          <h4>自选基本面画像（Top）</h4>
          <div v-if="!store.preReport.watchlist_profiles.length" class="placeholder">暂无画像</div>
          <table v-else class="tbl tbl-sm">
            <thead><tr>
              <th>代码</th><th>名称</th><th>行业</th><th>市值(亿)</th>
              <th>PE</th><th>ROE</th><th>营收增速</th><th>评级</th>
            </tr></thead>
            <tbody>
              <tr v-for="p in store.preReport.watchlist_profiles.slice(0, 10)" :key="p.code">
                <td>{{ p.code }}</td>
                <td>{{ p.name }}</td>
                <td>{{ p.industry || p.sector }}</td>
                <td>{{ (p.market_cap / 1e8).toFixed(1) }}</td>
                <td>{{ p.pe?.toFixed(2) ?? '—' }}</td>
                <td>{{ p.roe != null ? (p.roe * 100).toFixed(1) + '%' : '—' }}</td>
                <td>{{ p.revenue_growth != null ? (p.revenue_growth * 100).toFixed(1) + '%' : '—' }}</td>
                <td><span :class="'grade-badge-inline grade-' + p.grade">{{ p.grade }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="card span-2">
          <h4>大盘抽样 · 技术扫描亮点</h4>
          <div v-if="!store.preReport.broad_market_scan_top.length" class="placeholder">暂无技术扫描结果</div>
          <table v-else class="tbl">
            <thead><tr>
              <th>代码</th><th>名称</th><th>MA5/10/20/60</th><th>MACD 柱</th><th>RSI</th><th>KDJ K/D/J</th>
              <th>趋势</th><th>支撑 / 压力</th><th>信号</th>
            </tr></thead>
            <tbody>
              <tr v-for="t in store.preReport.broad_market_scan_top.slice(0, 15)" :key="t.code">
                <td>
                  {{ t.code }}
                  <span v-if="t.data_source === 'tdx_realtime'" class="src-tag src-tdx" title="tdx 实时数据">实时</span>
                </td>
                <td>{{ t.name || '—' }}</td>
                <td class="mono">
                  {{ t.ma5?.toFixed(2) ?? '—' }} / {{ t.ma10?.toFixed(2) ?? '—' }} /
                  {{ t.ma20?.toFixed(2) ?? '—' }} / {{ t.ma60?.toFixed(2) ?? '—' }}
                </td>
                <td>{{ t.macd_hist_last?.toFixed(3) ?? '—' }}</td>
                <td>{{ t.rsi_last?.toFixed(1) ?? '—' }}</td>
                <td class="mono">
                  {{ t.kdj_k_last?.toFixed(1) ?? '—' }} / {{ t.kdj_d_last?.toFixed(1) ?? '—' }} /
                  {{ t.kdj_j_last?.toFixed(1) ?? '—' }}
                </td>
                <td>
                  <span :class="'trend ' + t.trend_direction">
                    {{ t.trend_direction === 'up' ? '↑' : t.trend_direction === 'down' ? '↓' : '→' }}
                    {{ (t.trend_strength * 100).toFixed(0) }}%
                  </span>
                </td>
                <td class="mono">{{ t.support_levels.join(',') || '—' }} / {{ t.resistance_levels.join(',') || '—' }}</td>
                <td class="mono">{{ t.signals.join(' · ') || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      <div v-else class="placeholder">暂无盘前报告数据</div>
    </section>

    <!-- 消息面过滤列表 -->
    <section class="panel">
      <header class="panel-header">
        <div><h2>消息面列表</h2><p class="panel-sub">按重要性倒序，支持分类 + 关键词过滤</p></div>
        <div class="panel-actions">
          <select v-model="store.newsCategory" class="input input-xs" @change="store.loadNews(1)">
            <option v-for="o in categoryOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
          <input
            v-model="store.newsKeywords"
            placeholder="关键词（逗号分隔）"
            class="input input-xs"
            style="width: 220px"
            @keydown.enter="store.loadNews(1)"
          />
          <button class="btn btn-xs" @click="store.loadNews(1)">搜索</button>
        </div>
      </header>
      <div v-if="store.newsLoading" class="placeholder">加载中…</div>
      <table v-else-if="store.news" class="tbl">
        <thead><tr>
          <th style="width: 48px">重要</th>
          <th>标题</th>
          <th style="width: 80px">分类</th>
          <th style="width: 120px">来源</th>
          <th style="width: 180px">发布时间</th>
          <th style="width: 200px">匹配关键词</th>
        </tr></thead>
        <tbody>
          <tr v-for="n in store.news.items" :key="n.id">
            <td class="mono"><b>{{ n.importance }}</b>/10</td>
            <td>
              <a :href="n.url" target="_blank" rel="noreferrer">{{ n.title }}</a>
              <div v-if="n.summary" class="muted">{{ n.summary }}</div>
            </td>
            <td>
              <span :class="'tag tag-' + n.category">
                {{ n.category === 'news' ? '新闻' : n.category === 'announcement' ? '公告' : '政策' }}
              </span>
            </td>
            <td>{{ n.source }}</td>
            <td>{{ n.publish_ts }}</td>
            <td class="mono">{{ n.matched_keywords.join(' · ') || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <Pagination
        v-if="store.news && store.news.total > store.news.page_size"
        :page="store.news.page"
        :page-size="store.news.page_size"
        :total="store.news.total"
        :total-pages="store.news.total_pages"
        @change="store.loadNews($event)"
      />
    </section>

    <!-- 基本面画像列表 -->
    <section class="panel">
      <header class="panel-header">
        <div><h2>基本面画像</h2><p class="panel-sub">评级按 PE/PB/ROE/增速等综合，可按代码/等级过滤</p></div>
        <div class="panel-actions">
          <input v-model="store.fundamentalsCodes" placeholder="代码过滤（逗号分隔）" class="input input-xs" style="width: 220px" />
          <select v-model="store.fundamentalsGrade" class="input input-xs" @change="store.loadFundamentals(1)">
            <option v-for="o in gradeOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
          <button class="btn btn-xs" @click="store.loadFundamentals(1)">刷新</button>
          <input v-model="rerunFundamentalsInput" placeholder="手动重算代码(a,b)" class="input input-xs" style="width: 180px" />
          <button class="btn btn-xs" @click="store.rerunFundamentals(rerunFundamentalsInput)">重算画像</button>
        </div>
      </header>
      <div v-if="store.fundamentalsLoading" class="placeholder">加载中…</div>
      <table v-else-if="store.fundamentals" class="tbl">
        <thead><tr>
          <th>代码</th><th>名称</th><th>行业</th><th>市值(亿)</th>
          <th>PE</th><th>PB</th><th>ROE</th><th>营收增速</th><th>净利润增速</th><th>股息率</th><th>评级</th><th>摘要</th>
        </tr></thead>
        <tbody>
          <tr v-for="p in store.fundamentals.items" :key="p.code">
            <td>{{ p.code }}</td>
            <td>{{ p.name }}</td>
            <td>{{ p.industry || p.sector }}</td>
            <td class="mono">{{ (p.market_cap / 1e8).toFixed(1) }}</td>
            <td class="mono">{{ p.pe?.toFixed(2) ?? '—' }}</td>
            <td class="mono">{{ p.pb?.toFixed(2) ?? '—' }}</td>
            <td class="mono">{{ p.roe != null ? (p.roe * 100).toFixed(1) + '%' : '—' }}</td>
            <td class="mono">{{ p.revenue_growth != null ? (p.revenue_growth * 100).toFixed(1) + '%' : '—' }}</td>
            <td class="mono">{{ p.net_profit_growth != null ? (p.net_profit_growth * 100).toFixed(1) + '%' : '—' }}</td>
            <td class="mono">{{ p.dividend_yield != null ? (p.dividend_yield * 100).toFixed(2) + '%' : '—' }}</td>
            <td><span :class="'grade-badge-inline grade-' + p.grade">{{ p.grade }}</span></td>
            <td class="muted" style="min-width: 260px">
              <ul style="margin: 0; padding-left: 16px">
                <li v-for="(line, i) in p.summary_lines.slice(0, 3)" :key="i">{{ line }}</li>
              </ul>
            </td>
          </tr>
        </tbody>
      </table>
      <Pagination
        v-if="store.fundamentals && store.fundamentals.total > store.fundamentals.page_size"
        :page="store.fundamentals.page"
        :page-size="store.fundamentals.page_size"
        :total="store.fundamentals.total"
        :total-pages="store.fundamentals.total_pages"
        @change="store.loadFundamentals($event)"
      />
    </section>
  </div>
</template>

<style scoped>
.pre-open-view { display: flex; flex-direction: column; gap: 16px; }
.report-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}
.card.span-2 { grid-column: span 2; }
.news-list-sm { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 6px; }
.news-list-sm .muted { font-size: 12px; }
.trend.up { color: var(--up, #d9534f); font-weight: 600; }
.trend.down { color: var(--down, #3a8b3a); font-weight: 600; }
.trend.flat { color: var(--muted, #888); }
.tag-news { background: #f5f5f5; }
.tag-announcement { background: #e3f0ff; }
.tag-policy { background: #fff4d6; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.muted { color: var(--muted, #888); }
.src-tag { margin-left: 4px; font-size: 12px; padding: 1px 6px; border-radius: 4px; vertical-align: middle; }
.src-tdx { color: #2563eb; background: #eff6ff; border: 1px solid #bfdbfe; }
.grade-badge-inline {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 28px; height: 28px; padding: 0 6px;
  border-radius: 50%; font-weight: 700; color: #fff; font-size: 13px;
}
.grade-A { background: #1f8a3d; }
.grade-B { background: #3b7dd8; }
.grade-C { background: #e0a01d; }
.grade-D { background: #c94b4b; }
.grade-badge-inline.size-sm { min-width: 24px; height: 24px; font-size: 12px; }
</style>
