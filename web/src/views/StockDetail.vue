<script setup lang="ts">
/**
 * 个股详情页 /stock/:code（通达信风格）。
 * - 顶部：代码 + 名称 + 最新价 + 涨跌额/幅 + 今开/昨收/最高/最低/换手率
 * - 周期切换：日K / 周K / 月K / 年K / 60分钟 / 15分钟 / 5日
 * - 指标开关：MA / 成交量 / MACD / KDJ / RSI
 * - 主图 K 线 + 指标叠加（StockKlineChart）
 */
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api } from '../api';
import type { Period, StockDetail as StockDetailData } from '../types';
import StockKlineChart from '../components/StockKlineChart.vue';
import StateBlock from '../components/StateBlock.vue';

const route = useRoute();
const router = useRouter();

const code = computed(() => String(route.params.code || ''));
const name = computed(() => String(route.query.name || detail.value?.name || code.value));

const PERIODS: Array<{ key: Period; label: string }> = [
  { key: 'day', label: '日K' },
  { key: 'week', label: '周K' },
  { key: 'month', label: '月K' },
  { key: 'year', label: '年K' },
  { key: 'min60', label: '60分钟' },
  { key: 'min15', label: '15分钟' },
  { key: 'min5', label: '5日' },
];

/** 上市日期（后端自 lake 日线首根提取；用于图表动态起点与工具栏展示） */
const listingDate = computed(() => detail.value?.listing_date ?? '');

const period = ref<Period>('day');
const detail = ref<StockDetailData | null>(null);
const loading = ref(false);
const error = ref('');
const notFound = ref(false);

// 指标开关
const showMa = ref(true);
const showVolume = ref(true);
const showMacd = ref(true);
const showKdj = ref(false);
const showRsi = ref(false);

const UP = '#ef4444';
const DOWN = '#22c55e';

const last = computed(() => {
  if (!detail.value || detail.value.bars.length === 0) return null;
  return detail.value.bars[detail.value.bars.length - 1];
});

const prevClose = computed(() => {
  if (!detail.value || detail.value.bars.length < 2) return null;
  return detail.value.bars[detail.value.bars.length - 2].close;
});

const change = computed(() => {
  if (!last.value || !prevClose.value) return null;
  return {
    abs: last.value.close - prevClose.value,
    pct: ((last.value.close - prevClose.value) / prevClose.value) * 100,
  };
});

const color = computed(() => {
  if (!change.value) return '#222';
  return change.value.abs >= 0 ? UP : DOWN;
});

async function load(): Promise<void> {
  if (!code.value) return;
  loading.value = true;
  error.value = '';
  notFound.value = false;
  try {
    const env = await api.getStockDetail(code.value, {
      market: 'CN',
      period: period.value,
      limit: 600,
      indicators: 'ma,macd,kdj,rsi',
    });
    const d = env.data;
    if (!d.available || d.bars.length === 0) {
      notFound.value = true;
      detail.value = d;
      return;
    }
    detail.value = d;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

function switchPeriod(p: Period): void {
  period.value = p;
  void load();
}

function goBack(): void {
  router.back();
}

onMounted(load);
watch(code, () => void load());
</script>

<template>
  <div class="stock-detail">
    <!-- 头部信息条（通达信风格） -->
    <div class="sd-header">
      <button class="btn btn-ghost btn-sm" @click="goBack">← 返回</button>
      <div class="sd-title">
        <span class="sd-code">{{ code }}</span>
        <span class="sd-name">{{ name }}</span>
        <span class="sd-market">A股</span>
        <span
          v-if="detail?.data_source === 'tdx_realtime'"
          class="sd-src-tag"
          title="本地 market.db 无该标的日线，已自动回退通达信实时行情"
        >tdx 实时</span>
      </div>

      <template v-if="last && change">
        <div class="sd-price" :style="{ color }">
          <span class="sd-last">{{ last.close.toFixed(2) }}</span>
          <span class="sd-chg">
            {{ change.abs >= 0 ? '+' : '' }}{{ change.abs.toFixed(2) }}
            ({{ change.pct >= 0 ? '+' : '' }}{{ change.pct.toFixed(2) }}%)
          </span>
        </div>
        <div class="sd-quote-grid">
          <div><label>今开</label><span>{{ last.open.toFixed(2) }}</span></div>
          <div><label>昨收</label><span>{{ prevClose?.toFixed(2) }}</span></div>
          <div><label>最高</label><span :style="{ color: UP }">{{ last.high.toFixed(2) }}</span></div>
          <div><label>最低</label><span :style="{ color: DOWN }">{{ last.low.toFixed(2) }}</span></div>
          <div><label>成交量</label><span>{{ last.vol.toLocaleString('zh-CN') }}</span></div>
          <div><label>成交额</label><span>{{ last.amount.toLocaleString('zh-CN') }}</span></div>
          <div>
            <label>换手率</label>
            <span>
              {{ (last.turnover * 100).toFixed(2) }}%
              <em v-if="detail?.turnover_estimated" class="sd-est">估</em>
            </span>
          </div>
        </div>
      </template>
    </div>

    <!-- 周期切换 -->
    <div class="sd-toolbar">
      <div class="sd-periods">
        <button
          v-for="p in PERIODS"
          :key="p.key"
          class="period-btn"
          :class="{ active: period === p.key }"
          @click="switchPeriod(p.key)"
        >
          {{ p.label }}
        </button>
      </div>
      <div class="sd-toolbar-right">
        <span v-if="listingDate" class="sd-listing" :title="`上市日期（数据起点）：${listingDate}`">
          上市：{{ listingDate }}
        </span>
        <div class="sd-indicators">
          <label class="chk"><input type="checkbox" v-model="showMa" /> MA</label>
          <label class="chk"><input type="checkbox" v-model="showVolume" /> 成交量</label>
          <label class="chk"><input type="checkbox" v-model="showMacd" /> MACD</label>
          <label class="chk"><input type="checkbox" v-model="showKdj" /> KDJ</label>
          <label class="chk"><input type="checkbox" v-model="showRsi" /> RSI</label>
        </div>
      </div>
    </div>

    <!-- 图表区 -->
    <div class="sd-chart">
      <StateBlock
        v-if="loading"
        state="loading"
      />
      <div v-else-if="error" class="sd-error">
        <StateBlock state="error" :message="error" />
        <button class="btn btn-ghost btn-sm" @click="load">重试</button>
      </div>
      <StateBlock
        v-else-if="notFound"
        state="empty"
        :message="`${code} 暂无「${period}」数据` + (detail?.message ? `（${detail.message}）` : '')"
      />
      <StockKlineChart
        v-else-if="detail && detail.bars.length"
        :bars="detail.bars"
        :indicators="detail.indicators"
        :show-ma="showMa"
        :show-volume="showVolume"
        :show-macd="showMacd"
        :show-kdj="showKdj"
        :show-rsi="showRsi"
        :listing-date="listingDate"
        height="600px"
      />
    </div>
  </div>
</template>

<style scoped>
.stock-detail {
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.sd-header {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  padding: 8px 12px;
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
}
.sd-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.sd-code {
  font-family: var(--font-mono, monospace);
  font-size: 18px;
  font-weight: 700;
}
.sd-name {
  font-size: 18px;
  font-weight: 600;
}
.sd-market {
  font-size: 12px;
  color: var(--text-dim, #888);
  border: 1px solid var(--border, #ccc);
  border-radius: 4px;
  padding: 0 4px;
}
.sd-src-tag {
  font-size: 12px;
  color: #2563eb;
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  border-radius: 4px;
  padding: 0 6px;
}
.sd-price {
  display: flex;
  flex-direction: column;
  line-height: 1.1;
}
.sd-last {
  font-size: 24px;
  font-weight: 700;
  font-family: var(--font-mono, monospace);
}
.sd-chg {
  font-size: 13px;
}
.sd-quote-grid {
  display: grid;
  grid-template-columns: repeat(4, auto);
  gap: 2px 18px;
  font-size: 12px;
  color: var(--text-dim, #555);
}
.sd-quote-grid label {
  color: var(--text-dim, #999);
  margin-right: 4px;
}
.sd-quote-grid span {
  font-family: var(--font-mono, monospace);
  color: var(--text, #222);
}
.sd-est {
  color: var(--warn, #d97706);
  font-style: normal;
  font-size: 10px;
  border: 1px solid currentColor;
  border-radius: 3px;
  padding: 0 2px;
}
.sd-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 6px 12px;
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
}
.sd-periods {
  display: flex;
  gap: 4px;
}
.sd-toolbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.sd-listing {
  font-size: 12px;
  color: var(--text-dim, #888);
  font-family: var(--font-mono, monospace);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 4px;
  padding: 2px 6px;
  white-space: nowrap;
}
.period-btn {
  padding: 4px 12px;
  font-size: 13px;
  border: 1px solid var(--border, #d1d5db);
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
  color: var(--text, #333);
}
.period-btn.active {
  background: var(--accent, #2f6fed);
  color: #fff;
  border-color: var(--accent, #2f6fed);
}
.sd-indicators {
  display: flex;
  gap: 12px;
  font-size: 13px;
}
.chk {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
}
.sd-chart {
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
  min-height: 360px;
}
</style>
