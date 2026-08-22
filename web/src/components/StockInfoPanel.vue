<script setup lang="ts">
/**
 * 个股信息面板（StockInfoPanel）：通达信风格右侧栏。
 * - 实时行情卡片：当前价 + 涨跌额/幅 + 今开/昨收/最高/最低/换手/量比/PE/总市值
 * - 五档盘口：卖五→卖一 / 买一→买五（价格按昨收着色，量条背景）
 * - 逐笔成交明细：时间/价格/量/方向（15s 轮询，可滚动）
 * - 折叠面板：公司概况（股本/估值）、资金流向（主力/散户 + 5日大中小单）
 * - F10 入口（完整基本面页面规划中）
 * 数据来自 /stock/order-book /stock/transactions /stock/capital-flow（tdx 在线直连）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '../api';
import type { StockCapitalFlow, StockOrderBook, StockTransactions } from '../types';
import { fmtBig, fmtNumber } from '../utils/format';
import { toastInfo } from '../utils/toast';

const props = withDefaults(
  defineProps<{
    code: string;
    market?: string;
    name?: string;
  }>(),
  { market: 'CN', name: '' },
);

const POLL_MS = 15_000;

const orderBook = ref<StockOrderBook | null>(null);
const transactions = ref<StockTransactions | null>(null);
const capitalFlow = ref<StockCapitalFlow | null>(null);
const bookError = ref('');
const flowError = ref('');
const loading = ref(false);
let pollTimer: number | null = null;

async function fetchAll(): Promise<void> {
  loading.value = true;
  const [ob, tx, cf] = await Promise.allSettled([
    api.getStockOrderBook(props.code, { market: props.market }),
    api.getStockTransactions(props.code, { market: props.market, count: 300 }),
    api.getStockCapitalFlow(props.code, { market: props.market }),
  ]);
  // 轮询失败时保留上次数据（金融终端惯例：宁可显示旧盘口也不清空面板），
  // 仅在切换标的时由 watch 清空。
  if (ob.status === 'fulfilled') {
    orderBook.value = ob.value.data;
    bookError.value = '';
  } else if (orderBook.value === null) {
    bookError.value = ob.reason instanceof Error ? ob.reason.message : String(ob.reason);
  }
  if (tx.status === 'fulfilled') {
    transactions.value = tx.value.data;
  }
  if (cf.status === 'fulfilled') {
    capitalFlow.value = cf.value.data;
    flowError.value = '';
  } else if (capitalFlow.value === null) {
    flowError.value = cf.reason instanceof Error ? cf.reason.message : String(cf.reason);
  }
  loading.value = false;
}

const quote = computed(() => orderBook.value);
const change = computed(() => {
  const q = quote.value;
  if (!q || q.price == null || q.prev_close == null) return null;
  return q.price - q.prev_close;
});
const changePct = computed(() => {
  const q = quote.value;
  if (!q || q.price == null || q.prev_close == null || !q.prev_close) return null;
  return (q.price / q.prev_close - 1) * 100;
});
const rising = computed(() => (change.value ?? 0) >= 0);

/** 五档价格按昨收着色 */
function priceColor(p: number | null | undefined): string {
  const pc = quote.value?.prev_close;
  if (p == null || pc == null || !pc) return 'var(--text)';
  if (p > pc) return 'var(--red)';
  if (p < pc) return 'var(--green)';
  return 'var(--text)';
}

/** 量条宽度（相对五档最大挂单量） */
function volWidth(vol: number): number {
  const levels = [...(quote.value?.bids ?? []), ...(quote.value?.asks ?? [])];
  const max = Math.max(1, ...levels.map((l) => l.vol));
  return Math.round((vol / max) * 100);
}

/** 逐笔倒序展示（最新在上） */
const txReversed = computed(() => [...(transactions.value?.items ?? [])].reverse());

function txDirColor(bs: 0 | 1 | 2): string {
  if (bs === 0) return 'var(--red)';
  if (bs === 1) return 'var(--green)';
  return 'var(--text-secondary)';
}

function txDirLabel(bs: 0 | 1 | 2): string {
  if (bs === 0) return 'B';
  if (bs === 1) return 'S';
  return '—';
}

/* 折叠面板 */
const sections = ref<Record<string, boolean>>({ profile: false, capital: false });

function toggleSection(key: string): void {
  sections.value[key] = !sections.value[key];
}

function fmtYi(v: number | null | undefined): string {
  if (v == null) return '--';
  const yi = v / 1e8;
  if (Math.abs(yi) >= 1) return `${yi.toFixed(2)} 亿`;
  return fmtBig(v);
}

function signedYi(v: number | null | undefined): string {
  if (v == null) return '--';
  const yi = v / 1e8;
  const s = Math.abs(yi) >= 1 ? `${yi.toFixed(2)} 亿` : fmtBig(v);
  return v > 0 ? `+${s}` : s;
}

/** 流入/流出占比条（对侧为分母） */
function flowPct(a: number, b: number): number {
  const total = a + b;
  if (total <= 0) return 0;
  return Math.max(2, Math.round((a / total) * 100));
}

function f10(): void {
  toastInfo('F10 完整基本面页面规划中，敬请期待');
}

watch(
  () => props.code,
  () => {
    orderBook.value = null;
    transactions.value = null;
    capitalFlow.value = null;
    void fetchAll();
  },
);

onMounted(() => {
  void fetchAll();
  pollTimer = window.setInterval(() => void fetchAll(), POLL_MS);
});

onBeforeUnmount(() => {
  if (pollTimer !== null) window.clearInterval(pollTimer);
});
</script>

<template>
  <div class="info-panel">
    <!-- 实时行情卡片 -->
    <div class="quote-card">
      <template v-if="quote">
        <div class="qc-name-row">
          <span class="qc-name">{{ quote.name || name || code }}</span>
          <span class="qc-code">{{ code }}</span>
        </div>
        <div class="qc-price-row">
          <span class="qc-price" :class="rising ? 'rise' : 'fall'">
            {{ quote.price != null ? quote.price.toFixed(2) : '--' }}
          </span>
          <span class="qc-chg" :class="rising ? 'rise' : 'fall'">
            <template v-if="change != null">
              {{ rising ? '+' : '' }}{{ change.toFixed(2) }}
            </template>
            <template v-else>--</template>
            <span class="qc-chg-pct">
              {{ changePct != null ? `${rising ? '+' : ''}${changePct.toFixed(2)}%` : '--' }}
            </span>
          </span>
        </div>
        <div class="qc-grid">
          <div class="qc-item"><span>今开</span><b>{{ quote.open?.toFixed(2) ?? '--' }}</b></div>
          <div class="qc-item"><span>昨收</span><b>{{ quote.prev_close?.toFixed(2) ?? '--' }}</b></div>
          <div class="qc-item"><span>最高</span><b class="rise">{{ quote.high?.toFixed(2) ?? '--' }}</b></div>
          <div class="qc-item"><span>最低</span><b class="fall">{{ quote.low?.toFixed(2) ?? '--' }}</b></div>
          <div class="qc-item"><span>换手</span><b>{{ quote.turnover != null ? `${(quote.turnover * 100).toFixed(2)}%` : '--' }}</b></div>
          <div class="qc-item"><span>量比</span><b>{{ quote.vol_ratio != null ? quote.vol_ratio.toFixed(2) : '--' }}</b></div>
          <div class="qc-item"><span>成交额</span><b>{{ fmtBig(quote.amount) }}</b></div>
          <div class="qc-item"><span>总市值</span><b>{{ fmtYi(quote.total_market_cap) }}</b></div>
        </div>
      </template>
      <div v-else-if="loading" class="panel-hint">行情加载中…</div>
      <div v-else class="panel-hint panel-hint-error" :title="bookError">
        实时行情不可用（{{ bookError ? 'tdx 未配置或离线' : '' }}）
      </div>
    </div>

    <!-- 五档盘口 -->
    <div class="section">
      <div class="section-title">五档盘口</div>
      <template v-if="quote">
        <div class="book-rows">
          <div v-for="(lv, i) in [...(quote.asks ?? [])].reverse()" :key="`a${i}`" class="book-row">
            <span class="bk-label">卖{{ (quote.asks?.length ?? 0) - i }}</span>
            <span class="bk-price" :style="{ color: priceColor(lv.price) }">
              {{ lv.price != null ? lv.price.toFixed(2) : '--' }}
            </span>
            <span class="bk-vol">
              <i class="bk-bar" :style="{ width: `${volWidth(lv.vol)}%` }" />
              {{ fmtNumber(lv.vol, 0) }}
            </span>
          </div>
          <div class="book-divider" />
          <div v-for="(lv, i) in quote.bids ?? []" :key="`b${i}`" class="book-row">
            <span class="bk-label">买{{ i + 1 }}</span>
            <span class="bk-price" :style="{ color: priceColor(lv.price) }">
              {{ lv.price != null ? lv.price.toFixed(2) : '--' }}
            </span>
            <span class="bk-vol">
              <i class="bk-bar bid" :style="{ width: `${volWidth(lv.vol)}%` }" />
              {{ fmtNumber(lv.vol, 0) }}
            </span>
          </div>
        </div>
      </template>
      <div v-else class="panel-hint">盘口数据不可用</div>
    </div>

    <!-- 逐笔成交 -->
    <div class="section section-tx">
      <div class="section-title">
        逐笔成交
        <span class="section-sub">{{ transactions?.date || '' }}</span>
      </div>
      <div v-if="txReversed.length" class="tx-list">
        <div v-for="(t, i) in txReversed" :key="`${t.time}-${i}`" class="tx-row">
          <span class="tx-time">{{ t.time }}</span>
          <span class="tx-price" :style="{ color: txDirColor(t.bs) }">{{ t.price.toFixed(2) }}</span>
          <span class="tx-vol">{{ fmtNumber(t.vol, 0) }}</span>
          <span class="tx-dir" :style="{ color: txDirColor(t.bs) }">{{ txDirLabel(t.bs) }}</span>
        </div>
      </div>
      <div v-else class="panel-hint">暂无逐笔数据</div>
    </div>

    <!-- 折叠面板：公司概况 -->
    <div class="section collapsible">
      <button type="button" class="collapse-head" @click="toggleSection('profile')">
        <span>公司概况</span>
        <span class="collapse-arrow" :class="{ open: sections.profile }">▸</span>
      </button>
      <div v-if="sections.profile" class="collapse-body">
        <template v-if="quote">
          <div class="kv"><span>总股本</span><b>{{ fmtYi(quote.total_shares) }}</b></div>
          <div class="kv"><span>流通股本</span><b>{{ fmtYi(quote.float_shares) }}</b></div>
          <div class="kv"><span>PE(TTM)</span><b>{{ quote.pe_ttm != null ? quote.pe_ttm.toFixed(2) : '--' }}</b></div>
          <div class="kv"><span>PE(动)</span><b>{{ quote.pe_dynamic != null ? quote.pe_dynamic.toFixed(2) : '--' }}</b></div>
          <div class="kv"><span>PE(静)</span><b>{{ quote.pe_static != null ? quote.pe_static.toFixed(2) : '--' }}</b></div>
          <div class="kv"><span>EPS</span><b>{{ quote.eps != null ? quote.eps.toFixed(3) : '--' }}</b></div>
          <div class="kv"><span>每股净资产</span><b>{{ quote.net_assets != null ? quote.net_assets.toFixed(2) : '--' }}</b></div>
          <div class="kv">
            <span>市净率</span>
            <b>{{
              quote.net_assets && quote.price != null && quote.net_assets > 0
                ? (quote.price / quote.net_assets).toFixed(2)
                : '--'
            }}</b>
          </div>
          <div class="kv">
            <span>股息率</span>
            <b>{{ quote.dividend_yield != null ? `${(quote.dividend_yield * 100).toFixed(2)}%` : '--' }}</b>
          </div>
        </template>
        <div v-else class="panel-hint">暂无数据</div>
      </div>
    </div>

    <!-- 折叠面板：资金流向 -->
    <div class="section collapsible">
      <button type="button" class="collapse-head" @click="toggleSection('capital')">
        <span>资金流向</span>
        <span class="collapse-arrow" :class="{ open: sections.capital }">▸</span>
      </button>
      <div v-if="sections.capital" class="collapse-body">
        <template v-if="capitalFlow">
          <div class="flow-row">
            <div class="flow-item">
              <span>主力净流入</span>
              <b :class="capitalFlow.main_net >= 0 ? 'rise' : 'fall'">{{ signedYi(capitalFlow.main_net) }}</b>
            </div>
            <div class="flow-item">
              <span>散户净流入</span>
              <b :class="capitalFlow.small_net >= 0 ? 'rise' : 'fall'">{{ signedYi(capitalFlow.small_net) }}</b>
            </div>
          </div>
          <div class="flow-row">
            <div class="flow-item">
              <span>5日大单净额</span>
              <b :class="capitalFlow.large_net_5d >= 0 ? 'rise' : 'fall'">{{ signedYi(capitalFlow.large_net_5d) }}</b>
            </div>
            <div class="flow-item">
              <span>5日中单净额</span>
              <b :class="capitalFlow.mid_net_5d >= 0 ? 'rise' : 'fall'">{{ signedYi(capitalFlow.mid_net_5d) }}</b>
            </div>
          </div>
          <div class="flow-bars">
            <div class="flow-bar">
              <span>主力流入</span>
              <div class="bar-track"><i class="bar-fill rise-bg" :style="{ width: `${flowPct(capitalFlow.main_in, capitalFlow.main_out)}%` }" /></div>
              <b class="rise">{{ fmtYi(capitalFlow.main_in) }}</b>
            </div>
            <div class="flow-bar">
              <span>主力流出</span>
              <div class="bar-track"><i class="bar-fill fall-bg" :style="{ width: `${flowPct(capitalFlow.main_out, capitalFlow.main_in)}%` }" /></div>
              <b class="fall">{{ fmtYi(capitalFlow.main_out) }}</b>
            </div>
          </div>
        </template>
        <div v-else class="panel-hint" :title="flowError">资金流向数据不可用</div>
      </div>
    </div>

    <button type="button" class="f10-btn" @click="f10">F10 · 完整基本面</button>
  </div>
</template>

<style scoped>
.info-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
  overflow-y: auto;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px;
}
.quote-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
}
.qc-name-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.qc-name {
  font-weight: 600;
  font-size: 15px;
}
.qc-code {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text-faint);
}
.qc-price-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 4px 0 8px;
}
.qc-price {
  font-family: var(--mono);
  font-size: 30px;
  font-weight: 700;
  line-height: 1.1;
}
.qc-chg {
  font-family: var(--mono);
  font-size: 14px;
  font-weight: 600;
  display: flex;
  flex-direction: column;
}
.qc-chg-pct {
  font-size: 13px;
}
.rise {
  color: var(--red);
}
.fall {
  color: var(--green);
}
.qc-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 12px;
}
.qc-item {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
}
.qc-item span {
  color: var(--text-secondary);
}
.qc-item b {
  font-family: var(--mono);
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.section {
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
}
.section-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  font-weight: 600;
  padding: 6px 10px;
  background: var(--primary-weak);
  color: var(--primary);
}
.section-sub {
  font-weight: 400;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-faint);
}
.book-rows {
  padding: 6px 10px;
}
.book-row {
  display: grid;
  grid-template-columns: 34px 64px 1fr;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 2px 0;
  font-variant-numeric: tabular-nums;
}
.bk-label {
  color: var(--text-secondary);
}
.bk-price {
  font-family: var(--mono);
  font-weight: 500;
  text-align: right;
}
.bk-vol {
  position: relative;
  text-align: right;
  font-family: var(--mono);
  color: var(--text-secondary);
  padding-right: 4px;
}
.bk-bar {
  position: absolute;
  right: 0;
  top: 2px;
  bottom: 2px;
  background: rgba(220, 38, 38, 0.12);
  border-radius: 2px;
}
.bk-bar.bid {
  background: rgba(22, 163, 74, 0.12);
}
.book-divider {
  height: 1px;
  background: var(--border);
  margin: 4px 0;
}
.section-tx {
  display: flex;
  flex-direction: column;
  min-height: 140px;
  max-height: 260px;
}
.tx-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 10px;
}
.tx-row {
  display: grid;
  grid-template-columns: 56px 56px 1fr 16px;
  gap: 4px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  padding: 1px 0;
}
.tx-time {
  font-family: var(--mono);
  color: var(--text-faint);
}
.tx-price,
.tx-vol {
  font-family: var(--mono);
  text-align: right;
}
.tx-vol {
  color: var(--text-secondary);
}
.tx-dir {
  font-size: 11px;
  text-align: center;
}
.collapsible .collapse-head {
  display: flex;
  width: 100%;
  justify-content: space-between;
  align-items: center;
  border: none;
  background: var(--primary-weak);
  color: var(--primary);
  font-size: 12px;
  font-weight: 600;
  padding: 6px 10px;
  cursor: pointer;
}
.collapse-arrow {
  transition: transform 0.15s;
  font-size: 11px;
}
.collapse-arrow.open {
  transform: rotate(90deg);
}
.collapse-body {
  padding: 8px 10px;
}
.kv {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  padding: 2px 0;
}
.kv span {
  color: var(--text-secondary);
}
.kv b {
  font-family: var(--mono);
  font-weight: 500;
}
.flow-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-bottom: 8px;
}
.flow-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 11px;
}
.flow-item span {
  color: var(--text-secondary);
}
.flow-item b {
  font-family: var(--mono);
  font-size: 13px;
}
.flow-bars {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.flow-bar {
  display: grid;
  grid-template-columns: 56px 1fr auto;
  align-items: center;
  gap: 6px;
  font-size: 11px;
}
.flow-bar span {
  color: var(--text-secondary);
}
.bar-track {
  height: 6px;
  background: var(--border);
  border-radius: 999px;
  overflow: hidden;
}
.bar-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
}
.rise-bg {
  background: var(--red);
}
.fall-bg {
  background: var(--green);
}
.flow-bar b {
  font-family: var(--mono);
  font-weight: 500;
}
.f10-btn {
  border: 1px solid var(--border-strong);
  background: var(--panel);
  border-radius: 6px;
  padding: 7px;
  font-size: 12px;
  font-weight: 600;
  color: var(--primary);
  cursor: pointer;
}
.f10-btn:hover {
  border-color: var(--primary);
  background: var(--primary-weak);
}
.panel-hint {
  padding: 10px;
  text-align: center;
  color: var(--text-faint);
  font-size: 12px;
}
.panel-hint-error {
  color: var(--amber);
}
</style>
