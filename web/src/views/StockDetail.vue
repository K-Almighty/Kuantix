<script setup lang="ts">
/**
 * 个股详情页 /stock/:code（通达信风格）。
 * - 顶部：代码 + 名称 + 最新价 + 涨跌额/幅 + 今开/昨收/最高/最低/换手率
 * - 周期切换：日K / 周K / 月K / 年K / 60分钟 / 15分钟 / 分时
 *   （周期同步 URL query 并记忆，刷新/分享不丢状态）
 * - 指标开关：MA / 成交量 / MACD / KDJ / RSI（localStorage 记忆，
 *   切换不重置图表缩放）
 * - 主图 K 线 + 指标叠加（StockKlineChart）
 * - 请求带竞态保护：快速切换周期时丢弃过期响应
 */
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api } from '../api';
import type { Period, StockDetail as StockDetailData } from '../types';
import { fmtBig } from '../utils/format';
import StockKlineChart from '../components/StockKlineChart.vue';
import StateBlock from '../components/StateBlock.vue';

const route = useRoute();
const router = useRouter();

const code = computed(() => String(route.params.code || ''));
const name = computed(() => String(route.query.name || detail.value?.name || code.value));

const LS_KEY = 'kuantix.stock';

const PERIODS: Array<{ key: Period; label: string }> = [
  { key: 'day', label: '日K' },
  { key: 'week', label: '周K' },
  { key: 'month', label: '月K' },
  { key: 'year', label: '年K' },
  { key: 'min60', label: '60分钟' },
  { key: 'min15', label: '15分钟' },
  { key: 'min5', label: '分时' },
];

function isPeriod(v: unknown): v is Period {
  return typeof v === 'string' && PERIODS.some((p) => p.key === v);
}

/** 初始化周期：URL query 优先（可分享/刷新保持）> localStorage > 默认日K */
function initPeriod(): Period {
  if (isPeriod(route.query.period)) return route.query.period;
  const saved = localStorage.getItem(`${LS_KEY}.period`);
  if (isPeriod(saved)) return saved;
  return 'day';
}

/** 初始化持久化开关（localStorage 记忆用户偏好，缺省用默认值） */
function persistedFlag(key: string, def: boolean) {
  const raw = localStorage.getItem(`${LS_KEY}.${key}`);
  return ref(raw === null ? def : raw === '1');
}

const period = ref<Period>(initPeriod());
const detail = ref<StockDetailData | null>(null);
const loading = ref(false);
const error = ref('');
const notFound = ref(false);

/** 上市日期（后端自 lake 日线首根提取；用于图表动态起点与工具栏展示） */
const listingDate = computed(() => detail.value?.listing_date ?? '');

// 指标开关（刷新后保持用户上次选择）
const showMa = persistedFlag('showMa', true);
const showVolume = persistedFlag('showVolume', true);
const showMacd = persistedFlag('showMacd', true);
const showKdj = persistedFlag('showKdj', false);
const showRsi = persistedFlag('showRsi', false);

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

/**
 * 顶部报价区数据：优先用后端 quote（最新交易日行情，与当前周期无关）。
 * 周/月/年K的最后一根是跨期聚合值，直接展示会出现「单日 -27%」这类
 * 误导数字；quote 不可得时（旧后端/极端缺数据）退化为最后一根 K 线。
 */
const quoteView = computed(() => {
  const q = detail.value?.quote;
  if (q) {
    return {
      date: q.date,
      open: q.open,
      high: q.high,
      low: q.low,
      close: q.close,
      prevClose: q.prev_close,
      vol: q.vol,
      amount: q.amount,
      turnover: q.turnover,
    };
  }
  if (!last.value || !prevClose.value) return null;
  return {
    date: last.value.date,
    open: last.value.open,
    high: last.value.high,
    low: last.value.low,
    close: last.value.close,
    prevClose: prevClose.value,
    vol: last.value.vol,
    amount: last.value.amount,
    turnover: last.value.turnover,
  };
});

const change = computed(() => {
  const q = quoteView.value;
  if (!q || !q.prevClose) return null;
  return {
    abs: q.close - q.prevClose,
    pct: ((q.close - q.prevClose) / q.prevClose) * 100,
  };
});

const color = computed(() => {
  if (!change.value) return '#222';
  return change.value.abs >= 0 ? UP : DOWN;
});

/** 请求序号：快速切换周期/标的时丢弃过期响应，防止旧数据覆盖新数据 */
let loadSeq = 0;

async function load(): Promise<void> {
  if (!code.value) return;
  const seq = ++loadSeq;
  loading.value = true;
  error.value = '';
  notFound.value = false;
  try {
    const env = await api.getStockDetail(code.value, {
      market: 'CN',
      period: period.value,
      // 分时（5 日 1 分钟）需完整窗口 1200 根；其余周期 600 根足够
      limit: period.value === 'min5' ? 1500 : 600,
      indicators: 'ma,macd,kdj,rsi',
    });
    if (seq !== loadSeq) return;
    const d = env.data;
    if (!d.available || d.bars.length === 0) {
      notFound.value = true;
      detail.value = d;
      return;
    }
    detail.value = d;
  } catch (e) {
    if (seq !== loadSeq) return;
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
}

// 周期变化：持久化 + 同步到 URL query（刷新/分享不丢状态）+ 重新加载
watch(period, (p) => {
  localStorage.setItem(`${LS_KEY}.period`, p);
  void router.replace({ query: { ...route.query, period: p } });
  void load();
});

// 指标开关变化：持久化（无需重新请求，图表组件本地响应）
watch([showMa, showVolume, showMacd, showKdj, showRsi], ([ma, vol, macd, kdj, rsi]) => {
  localStorage.setItem(`${LS_KEY}.showMa`, ma ? '1' : '0');
  localStorage.setItem(`${LS_KEY}.showVolume`, vol ? '1' : '0');
  localStorage.setItem(`${LS_KEY}.showMacd`, macd ? '1' : '0');
  localStorage.setItem(`${LS_KEY}.showKdj`, kdj ? '1' : '0');
  localStorage.setItem(`${LS_KEY}.showRsi`, rsi ? '1' : '0');
});

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

      <template v-if="quoteView && change">
        <div class="sd-price" :style="{ color }" :title="`行情日期：${quoteView.date}`">
          <span class="sd-last">{{ quoteView.close.toFixed(2) }}</span>
          <span class="sd-chg">
            {{ change.abs >= 0 ? '+' : '' }}{{ change.abs.toFixed(2) }}
            ({{ change.pct >= 0 ? '+' : '' }}{{ change.pct.toFixed(2) }}%)
          </span>
        </div>
        <div class="sd-quote-grid">
          <div><label>今开</label><span>{{ quoteView.open.toFixed(2) }}</span></div>
          <div><label>昨收</label><span>{{ quoteView.prevClose?.toFixed(2) }}</span></div>
          <div><label>最高</label><span :style="{ color: UP }">{{ quoteView.high.toFixed(2) }}</span></div>
          <div><label>最低</label><span :style="{ color: DOWN }">{{ quoteView.low.toFixed(2) }}</span></div>
          <div><label>成交量</label><span>{{ fmtBig(quoteView.vol) }}</span></div>
          <div><label>成交额</label><span>{{ fmtBig(quoteView.amount) }}</span></div>
          <div>
            <label>换手率</label>
            <span>
              <template v-if="quoteView.turnover > 0">
                {{ (quoteView.turnover * 100).toFixed(2) }}%
                <em v-if="detail?.turnover_estimated" class="sd-est">估</em>
              </template>
              <template v-else>--</template>
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
          @click="period = p.key"
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
        :period="period"
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
