<script setup lang="ts">
/**
 * 个股详情页 /stock/:code（通达信风格三栏终端布局）。
 * - 左栏（220px）：自选股侧栏（分组/搜索添加/拖拽排序/实时报价）
 * - 中栏：标的报价条 + 工具栏（周期/复权/指标/画线/同屏/主题）+ 图表区
 *   · 单图：K线（StockKlineChart）或分时（StockTimeShareChart，period=min1）
 *   · 多周期同屏：2/4 个周期窗口并排对比（通达信特色）
 * - 右栏（300px）：信息面板（行情卡片/五档盘口/逐笔明细/折叠面板/F10）
 * - 键盘精灵：任意位置输入代码/名称首字母快速切换标的
 * - 状态记忆：周期/复权同步 URL query；指标开关、布局 localStorage 持久化
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api } from '../api';
import type { Adjust, DrawTool, Period, StockDetail as StockDetailData } from '../types';
import type { SecurityHit } from '../types/data';
import { fmtBig } from '../utils/format';
import { toastWarning } from '../utils/toast';
import StockKlineChart from '../components/StockKlineChart.vue';
import StockTimeShareChart from '../components/StockTimeShareChart.vue';
import StockWatchlistSidebar from '../components/StockWatchlistSidebar.vue';
import StockInfoPanel from '../components/StockInfoPanel.vue';
import StateBlock from '../components/StateBlock.vue';

const route = useRoute();
const router = useRouter();

const code = computed(() => String(route.params.code || ''));

const LS_KEY = 'kuantix.stock';

const PERIODS: Array<{ key: Period; label: string }> = [
  { key: 'min1', label: '分时' },
  { key: 'min5', label: '5分' },
  { key: 'min15', label: '15分' },
  { key: 'min30', label: '30分' },
  { key: 'min60', label: '60分' },
  { key: 'day', label: '日K' },
  { key: 'week', label: '周K' },
  { key: 'month', label: '月K' },
  { key: 'quarter', label: '季K' },
  { key: 'year', label: '年K' },
];

const ADJUSTS: Array<{ key: Adjust; label: string }> = [
  { key: 'none', label: '不复权' },
  { key: 'qfq', label: '前复权' },
  { key: 'hfq', label: '后复权' },
];

const DRAW_TOOLS: Array<{ key: DrawTool; label: string }> = [
  { key: 'trend', label: '趋势线' },
  { key: 'hline', label: '水平线' },
  { key: 'rect', label: '矩形' },
  { key: 'fib', label: '黄金分割' },
  { key: 'text', label: '文本' },
];

function isPeriod(v: unknown): v is Period {
  return typeof v === 'string' && PERIODS.some((p) => p.key === v);
}
function isAdjust(v: unknown): v is Adjust {
  return typeof v === 'string' && ADJUSTS.some((a) => a.key === v);
}
function isMinute(p: Period): boolean {
  return p.startsWith('min');
}

/** localStorage 持久化开关 */
function persistedFlag(key: string, def: boolean): ReturnType<typeof ref<boolean>> {
  const raw = localStorage.getItem(`${LS_KEY}.${key}`);
  return ref(raw === null ? def : raw === '1');
}

function initPeriod(): Period {
  if (isPeriod(route.query.period)) return route.query.period;
  const saved = localStorage.getItem(`${LS_KEY}.period`);
  if (isPeriod(saved)) return saved;
  return 'day';
}
function initAdjust(): Adjust {
  if (isAdjust(route.query.adjust)) return route.query.adjust;
  const saved = localStorage.getItem(`${LS_KEY}.adjust`);
  if (isAdjust(saved)) return saved;
  return 'none';
}

const period = ref<Period>(initPeriod());
const adjust = ref<Adjust>(initAdjust());

// 主图指标开关
const showMa = persistedFlag('showMa', true);
const showBoll = persistedFlag('showBoll', false);
const showEne = persistedFlag('showEne', false);
const showSar = persistedFlag('showSar', false);
// 副图指标开关
const showVolume = persistedFlag('showVolume', true);
const showMacd = persistedFlag('showMacd', true);
const showKdj = persistedFlag('showKdj', false);
const showRsi = persistedFlag('showRsi', false);
const showWr = persistedFlag('showWr', false);
const showBias = persistedFlag('showBias', false);
const showObv = persistedFlag('showObv', false);

// MA 参数（自定义窗口）
function initMaWindows(): number[] {
  const raw = localStorage.getItem(`${LS_KEY}.maWindows`);
  if (raw) {
    try {
      const arr = JSON.parse(raw) as unknown[];
      const nums = arr.filter((v) => typeof v === 'number' && v >= 1 && v <= 500).map(Number);
      if (nums.length >= 1 && nums.length <= 6) return [...new Set(nums)].sort((a, b) => a - b);
    } catch {
      /* 忽略损坏数据 */
    }
  }
  return [5, 10, 20, 60];
}
const maWindows = ref<number[]>(initMaWindows());
const maKey = computed(() => maWindows.value.join(','));
const maModalOpen = ref(false);
const maInput = ref('');

// 布局 / 配色（红涨绿跌为固定默认，A股习惯）
const showSidebar = persistedFlag('showSidebar', true);
const showPanel = persistedFlag('showPanel', true);
const multiCount = ref<number>(Number(localStorage.getItem(`${LS_KEY}.multiCount`)) || 0);

const upColor = '#ef4444';
const downColor = '#22c55e';

const drawTool = ref<DrawTool>('none');

/* ---------------- 数据加载（带缓存 + 竞态保护） ---------------- */

/** key = code|period|adjust|maWindows */
const details = reactive(new Map<string, StockDetailData>());
const pending = reactive(new Map<string, boolean>());
const errors = reactive(new Map<string, string>());

function keyOf(p: Period): string {
  return `${code.value}|${p}|${adjust.value}|${maKey.value}`;
}

async function ensure(p: Period): Promise<void> {
  if (!code.value) return;
  const key = keyOf(p);
  if (details.has(key) || pending.get(key)) return;
  pending.set(key, true);
  errors.delete(key);
  try {
    const env = await api.getStockDetail(code.value, {
      market: 'CN',
      period: p,
      adjust: adjust.value,
      limit: isMinute(p) ? 1500 : 600,
      ma: maKey.value,
    });
    // 标的/复权/MA 参数在请求期间变更 → 丢弃过期响应
    if (key !== keyOf(p)) return;
    details.set(key, env.data);
    // 缓存上限：FIFO 淘汰最旧
    if (details.size > 12) {
      const oldest = details.keys().next().value;
      if (oldest !== undefined && oldest !== key) details.delete(oldest);
    }
  } catch (e) {
    if (key === keyOf(p)) errors.set(key, e instanceof Error ? e.message : String(e));
  } finally {
    pending.delete(key);
  }
}

const mainDetail = computed(() => details.get(keyOf(period.value)) ?? null);
const mainPending = computed(() => pending.get(keyOf(period.value)) === true);
const mainError = computed(() => errors.get(keyOf(period.value)) ?? '');
const mainNotFound = computed(
  () => mainDetail.value !== null && (!mainDetail.value.available || mainDetail.value.bars.length === 0),
);

const name = computed(() => String(route.query.name || mainDetail.value?.name || code.value));
const listingDate = computed(() => mainDetail.value?.listing_date ?? '');

/* ---------------- 顶部报价区（与周期无关的日口径） ---------------- */

const quoteView = computed(() => {
  const q = mainDetail.value?.quote;
  if (!q) return null;
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
});

const change = computed(() => {
  const q = quoteView.value;
  if (!q || !q.prevClose) return null;
  return { abs: q.close - q.prevClose, pct: ((q.close - q.prevClose) / q.prevClose) * 100 };
});

const headColor = computed(() => {
  if (!change.value) return 'var(--text)';
  return change.value.abs >= 0 ? upColor : downColor;
});

/* ---------------- 多周期同屏 ---------------- */

function initSlotPeriods(): Period[] {
  if (multiCount.value === 2) return [period.value === 'min1' ? 'day' : period.value, 'min60'];
  if (multiCount.value === 4) return [
    period.value === 'min1' ? 'day' : period.value,
    'min60',
    'min30',
    'min15',
  ];
  return [];
}
const slotPeriods = ref<Period[]>(initSlotPeriods());

function setMulti(n: number): void {
  multiCount.value = n;
  localStorage.setItem(`${LS_KEY}.multiCount`, String(n));
  slotPeriods.value =
    n === 2
      ? [period.value === 'min1' ? 'day' : period.value, 'min60']
      : n === 4
        ? [period.value === 'min1' ? 'day' : period.value, 'min60', 'min30', 'min15']
        : [];
}

function slotState(i: number): {
  detail: StockDetailData | null;
  loading: boolean;
  error: string;
  notFound: boolean;
} {
  const p = slotPeriods.value[i] ?? 'day';
  const key = keyOf(p);
  const d = details.get(key) ?? null;
  return {
    detail: d,
    loading: pending.get(key) === true,
    error: errors.get(key) ?? '',
    notFound: d !== null && (!d.available || d.bars.length === 0),
  };
}

function ensureAll(): void {
  if (multiCount.value === 0) {
    void ensure(period.value);
  } else {
    slotPeriods.value.forEach((p) => void ensure(p));
  }
}

/* ---------------- 图表高度（副图开关数量自适应） ---------------- */

const subPanelCount = computed(
  () =>
    [
      showVolume.value,
      showMacd.value,
      showKdj.value,
      showRsi.value,
      showWr.value,
      showBias.value,
      showObv.value,
    ].filter(Boolean).length,
);

const chartHeight = computed(() => {
  if (multiCount.value !== 0) return '400px';
  const extra = Math.max(0, subPanelCount.value - 2) * 80;
  return `${520 + extra}px`;
});

/* ---------------- 画线 / 全屏 ---------------- */

const mainChartRef = ref<
  InstanceType<typeof StockKlineChart> | InstanceType<typeof StockTimeShareChart> | null
>(null);

function clearDrawings(): void {
  mainChartRef.value?.clearDrawings();
}

const mainCol = ref<HTMLElement | null>(null);

function toggleFullscreen(): void {
  if (!document.fullscreenElement) {
    void mainCol.value?.requestFullscreen();
  } else {
    void document.exitFullscreen();
  }
}

/* ---------------- 键盘精灵 ---------------- */

const spriteOpen = ref(false);
const spriteQuery = ref('');
const spriteHits = ref<SecurityHit[]>([]);
const spriteSearching = ref(false);
const spriteInput = ref<HTMLInputElement | null>(null);
let spriteTimer: number | null = null;

function onGlobalKeydown(e: KeyboardEvent): void {
  const t = e.target as HTMLElement | null;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) {
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'Escape') {
    spriteOpen.value = false;
    return;
  }
  if (e.key.length === 1 && /[0-9a-zA-Z]/.test(e.key)) {
    spriteOpen.value = true;
    spriteQuery.value = (spriteQuery.value + e.key).slice(0, 12);
    e.preventDefault();
  }
}

watch(spriteOpen, (open) => {
  if (open) {
    void nextTick(() => spriteInput.value?.focus());
  } else {
    spriteQuery.value = '';
    spriteHits.value = [];
  }
});

watch(spriteQuery, (q) => {
  if (spriteTimer !== null) window.clearTimeout(spriteTimer);
  if (!q.trim()) {
    spriteHits.value = [];
    return;
  }
  spriteTimer = window.setTimeout(async () => {
    spriteSearching.value = true;
    try {
      const env = await api.searchSecurities(q.trim(), 'CN', 12);
      spriteHits.value = env.data.items;
    } catch {
      spriteHits.value = [];
    } finally {
      spriteSearching.value = false;
    }
  }, 250);
});

function spriteGoto(hit: SecurityHit): void {
  spriteOpen.value = false;
  void router.push({
    path: `/stock/${hit.code}`,
    query: { name: hit.name, period: period.value, adjust: adjust.value },
  });
}

function spriteEnter(): void {
  if (spriteHits.value.length > 0) spriteGoto(spriteHits.value[0]);
}

/* ---------------- MA 参数面板 ---------------- */

function openMaModal(): void {
  maInput.value = maWindows.value.join(',');
  maModalOpen.value = true;
}

function saveMa(): void {
  const nums = [
    ...new Set(
      maInput.value
        .split(/[,，\s]+/)
        .map((s) => Number(s.trim()))
        .filter((n) => Number.isInteger(n) && n >= 1 && n <= 500),
    ),
  ].sort((a, b) => a - b);
  if (nums.length < 1 || nums.length > 6) {
    toastWarning('MA 窗口需 1-6 个（每个 1-500 的整数）');
    return;
  }
  maWindows.value = nums;
  localStorage.setItem(`${LS_KEY}.maWindows`, JSON.stringify(nums));
  maModalOpen.value = false;
}

/* ---------------- 持久化与联动 ---------------- */

watch(period, (p) => {
  localStorage.setItem(`${LS_KEY}.period`, p);
  void router.replace({ query: { ...route.query, period: p } });
});
watch(adjust, (a) => {
  localStorage.setItem(`${LS_KEY}.adjust`, a);
  void router.replace({ query: { ...route.query, adjust: a } });
});

const flagMap: Record<string, ReturnType<typeof ref<boolean>>> = {
  showMa,
  showBoll,
  showEne,
  showSar,
  showVolume,
  showMacd,
  showKdj,
  showRsi,
  showWr,
  showBias,
  showObv,
  showSidebar,
  showPanel,
};
watch(
  () => Object.entries(flagMap).map(([, r]) => r.value),
  (vals) => {
    Object.keys(flagMap).forEach((k, i) => {
      localStorage.setItem(`${LS_KEY}.${k}`, vals[i] ? '1' : '0');
    });
  },
);

watch([code, adjust, maKey, period], () => void ensureAll());
watch(slotPeriods, () => void ensureAll(), { deep: true });

onMounted(() => {
  window.addEventListener('keydown', onGlobalKeydown);
  ensureAll();
});

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onGlobalKeydown);
  if (spriteTimer !== null) window.clearTimeout(spriteTimer);
});
</script>

<template>
  <div class="stock-page">
    <!-- 键盘精灵（任意位置敲代码/字母唤起） -->
    <Teleport to="body">
      <div v-if="spriteOpen" class="sprite-mask" @click.self="spriteOpen = false">
        <div class="sprite-box">
          <input
            ref="spriteInput"
            v-model="spriteQuery"
            class="sprite-input"
            placeholder="输入代码 / 名称 / 拼音首字母，Enter 确认，Esc 关闭"
            @keydown.enter.prevent="spriteEnter"
            @keydown.esc.prevent="spriteOpen = false"
          />
          <div v-if="spriteSearching" class="sprite-hint">搜索中…</div>
          <ul v-else-if="spriteHits.length" class="sprite-list">
            <li v-for="hit in spriteHits" :key="hit.exchange + hit.code">
              <button type="button" class="sprite-option" @click="spriteGoto(hit)">
                <span class="so-code">{{ hit.code }}</span>
                <span class="so-name">{{ hit.name }}</span>
                <span class="so-meta">{{ hit.exchange.toUpperCase() }} · {{ hit.security_type }}</span>
              </button>
            </li>
          </ul>
          <div v-else-if="spriteQuery.trim()" class="sprite-hint">无匹配证券</div>
        </div>
      </div>
    </Teleport>

    <!-- MA 参数设置弹窗 -->
    <Teleport to="body">
      <div v-if="maModalOpen" class="modal-mask" @click.self="maModalOpen = false">
        <div class="modal ma-modal">
          <h3>指标参数设置</h3>
          <div class="field">
            <label>MA 均线窗口（逗号分隔，1-6 个，每个 1-500）</label>
            <input v-model="maInput" class="input" placeholder="如 5,10,20,60" />
          </div>
          <p class="ma-hint">
            MACD(12,26,9)、KDJ(9,3,3)、BOLL(20,2)、RSI(6,12,24) 等暂为通达信默认参数。
          </p>
          <div class="ma-actions">
            <button class="btn" @click="maModalOpen = false">取消</button>
            <button class="btn btn-primary" @click="saveMa">保存</button>
          </div>
        </div>
      </div>
    </Teleport>

    <div class="stock-layout">
      <!-- 左栏：自选股 -->
      <aside v-if="showSidebar" class="col-sidebar">
        <StockWatchlistSidebar :active-code="code" />
      </aside>

      <!-- 中栏：报价条 + 工具栏 + 图表 -->
      <main ref="mainCol" class="col-main">
        <!-- 标的报价条 -->
        <div class="head-bar">
          <div class="hb-title">
            <span class="hb-code">{{ code }}</span>
            <span class="hb-name">{{ name }}</span>
            <span class="hb-market">A股</span>
            <span
              v-if="mainDetail?.data_source === 'tdx_realtime'"
              class="hb-src-tag"
              title="本地 market.db 无该标的数据，已自动回退通达信实时行情"
            >tdx 实时</span>
            <span v-if="listingDate" class="hb-listing" :title="`上市日期：${listingDate}`">
              上市：{{ listingDate }}
            </span>
          </div>
          <template v-if="quoteView && change">
            <div class="hb-price" :style="{ color: headColor }">
              <span class="hb-last">{{ quoteView.close.toFixed(2) }}</span>
              <span class="hb-chg">
                {{ change.abs >= 0 ? '+' : '' }}{{ change.abs.toFixed(2) }}
                ({{ change.pct >= 0 ? '+' : '' }}{{ change.pct.toFixed(2) }}%)
              </span>
            </div>
            <div class="hb-grid">
              <div><label>今开</label><span>{{ quoteView.open.toFixed(2) }}</span></div>
              <div><label>昨收</label><span>{{ quoteView.prevClose?.toFixed(2) ?? '--' }}</span></div>
              <div><label>最高</label><span :style="{ color: upColor }">{{ quoteView.high.toFixed(2) }}</span></div>
              <div><label>最低</label><span :style="{ color: downColor }">{{ quoteView.low.toFixed(2) }}</span></div>
              <div><label>成交量</label><span>{{ fmtBig(quoteView.vol) }}</span></div>
              <div><label>成交额</label><span>{{ fmtBig(quoteView.amount) }}</span></div>
              <div>
                <label>换手率</label>
                <span>
                  <template v-if="quoteView.turnover > 0">{{ (quoteView.turnover * 100).toFixed(2) }}%</template>
                  <template v-else>--</template>
                </span>
              </div>
            </div>
          </template>
        </div>

        <!-- 工具栏 -->
        <div class="toolbar">
          <div class="tb-row">
            <template v-if="multiCount === 0">
              <div class="tb-group">
                <button
                  v-for="p in PERIODS"
                  :key="p.key"
                  class="tb-btn"
                  :class="{ active: period === p.key }"
                  @click="period = p.key"
                >
                  {{ p.label }}
                </button>
              </div>
            </template>
            <div class="tb-group">
              <select v-model="adjust" class="select tb-select" title="复权方式">
                <option v-for="a in ADJUSTS" :key="a.key" :value="a.key">{{ a.label }}</option>
              </select>
            </div>
            <div class="tb-group">
              <button
                v-for="n in [2, 4]"
                :key="n"
                class="tb-btn"
                :class="{ active: multiCount === n }"
                :title="`${n} 周期同屏对比`"
                @click="setMulti(multiCount === n ? 0 : n)"
              >
                {{ n }}屏
              </button>
              <button
                class="tb-btn"
                :class="{ active: multiCount === 0 }"
                title="单图模式"
                @click="setMulti(0)"
              >
                单图
              </button>
            </div>
            <div class="tb-spacer" />
            <div class="tb-group">
              <button class="tb-btn" title="折叠/展开自选股侧栏" @click="showSidebar = !showSidebar">
                自选
              </button>
              <button class="tb-btn" title="折叠/展开信息面板" @click="showPanel = !showPanel">
                面板
              </button>
              <button class="tb-btn" title="全屏图表区" @click="toggleFullscreen">全屏</button>
            </div>
          </div>
          <div class="tb-row tb-row-second">
            <div class="tb-group" title="主图指标">
              <span class="tb-label">主图</span>
              <label class="chk"><input v-model="showMa" type="checkbox" />MA</label>
              <label class="chk"><input v-model="showBoll" type="checkbox" />BOLL</label>
              <label class="chk"><input v-model="showEne" type="checkbox" />ENE</label>
              <label class="chk"><input v-model="showSar" type="checkbox" />SAR</label>
              <button class="tb-btn tb-btn-xs" title="指标参数设置" @click="openMaModal">参数</button>
            </div>
            <div class="tb-group" title="副图指标">
              <span class="tb-label">副图</span>
              <label class="chk"><input v-model="showVolume" type="checkbox" />VOL</label>
              <label class="chk"><input v-model="showMacd" type="checkbox" />MACD</label>
              <label class="chk"><input v-model="showKdj" type="checkbox" />KDJ</label>
              <label class="chk"><input v-model="showRsi" type="checkbox" />RSI</label>
              <label class="chk"><input v-model="showWr" type="checkbox" />WR</label>
              <label class="chk"><input v-model="showBias" type="checkbox" />BIAS</label>
              <label class="chk"><input v-model="showObv" type="checkbox" />OBV</label>
            </div>
            <div v-if="multiCount === 0" class="tb-group" title="画线工具">
              <span class="tb-label">画线</span>
              <button
                v-for="t in DRAW_TOOLS"
                :key="t.key"
                class="tb-btn tb-btn-xs"
                :class="{ active: drawTool === t.key }"
                @click="drawTool = drawTool === t.key ? 'none' : t.key"
              >
                {{ t.label }}
              </button>
              <button class="tb-btn tb-btn-xs" title="清空全部画线" @click="clearDrawings">清除</button>
            </div>
          </div>
        </div>

        <!-- 图表区 -->
        <div class="charts-area" :class="multiCount === 2 ? 'two' : multiCount === 4 ? 'four' : 'one'">
          <template v-if="multiCount === 0">
            <div class="chart-cell">
              <StateBlock v-if="mainPending" state="loading" />
              <div v-else-if="mainError" class="chart-state">
                <StateBlock state="error" :message="mainError" />
                <button class="btn btn-ghost btn-sm" @click="ensureAll">重试</button>
              </div>
              <StateBlock
                v-else-if="mainNotFound"
                state="empty"
                :message="`${code} 暂无「${period}」数据` + (mainDetail?.message ? `（${mainDetail.message}）` : '')"
              />
              <StockTimeShareChart
                v-else-if="period === 'min1' && mainDetail"
                ref="mainChartRef"
                v-model:draw-tool="drawTool"
                :bars="mainDetail.bars"
                :vwap="mainDetail.indicators.vwap ?? []"
                :prev-close="mainDetail.quote?.prev_close ?? null"
                :height="chartHeight"
                :up-color="upColor"
                :down-color="downColor"
              />
              <StockKlineChart
                v-else-if="mainDetail"
                ref="mainChartRef"
                v-model:draw-tool="drawTool"
                :bars="mainDetail.bars"
                :indicators="mainDetail.indicators"
                :period="period"
                :show-ma="showMa"
                :show-boll="showBoll"
                :show-ene="showEne"
                :show-sar="showSar"
                :ma-windows="maWindows"
                :show-volume="showVolume"
                :show-macd="showMacd"
                :show-kdj="showKdj"
                :show-rsi="showRsi"
                :show-wr="showWr"
                :show-bias="showBias"
                :show-obv="showObv"
                :up-color="upColor"
                :down-color="downColor"
                :listing-date="listingDate"
                :height="chartHeight"
              />
            </div>
          </template>
          <template v-else>
            <div v-for="i in slotPeriods.length" :key="i" class="chart-cell">
              <div class="slot-head">
                <select v-model="slotPeriods[i - 1]" class="select slot-select">
                  <option v-for="p in PERIODS" :key="p.key" :value="p.key">{{ p.label }}</option>
                </select>
                <span
                  v-if="slotState(i - 1).detail?.data_source === 'tdx_realtime'"
                  class="slot-tag"
                >tdx 实时</span>
              </div>
              <StateBlock v-if="slotState(i - 1).loading" state="loading" />
              <StateBlock
                v-else-if="slotState(i - 1).error"
                state="error"
                :message="slotState(i - 1).error"
              />
              <StateBlock
                v-else-if="slotState(i - 1).notFound"
                state="empty"
                :message="`${code} 暂无「${slotPeriods[i - 1]}」数据`"
              />
              <StockTimeShareChart
                v-else-if="slotPeriods[i - 1] === 'min1' && slotState(i - 1).detail"
                :bars="slotState(i - 1).detail!.bars"
                :vwap="slotState(i - 1).detail!.indicators.vwap ?? []"
                :prev-close="slotState(i - 1).detail!.quote?.prev_close ?? null"
                height="340px"
                :up-color="upColor"
                :down-color="downColor"
              />
              <StockKlineChart
                v-else-if="slotState(i - 1).detail"
                :bars="slotState(i - 1).detail!.bars"
                :indicators="slotState(i - 1).detail!.indicators"
                :period="slotPeriods[i - 1]"
                :show-ma="showMa"
                :show-boll="showBoll"
                :show-ene="showEne"
                :show-sar="showSar"
                :ma-windows="maWindows"
                :show-volume="showVolume"
                :show-macd="showMacd"
                :show-kdj="showKdj"
                :show-rsi="showRsi"
                :show-wr="showWr"
                :show-bias="showBias"
                :show-obv="showObv"
                :up-color="upColor"
                :down-color="downColor"
                :listing-date="slotState(i - 1).detail!.listing_date ?? ''"
                height="340px"
              />
            </div>
          </template>
        </div>
      </main>

      <!-- 右栏：信息面板 -->
      <aside v-if="showPanel" class="col-panel">
        <StockInfoPanel :code="code" :name="name" />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.stock-page {
  padding: 10px 12px;
}
.stock-layout {
  display: flex;
  gap: 10px;
  align-items: stretch;
}
.col-sidebar {
  width: 220px;
  flex: none;
  min-height: calc(100vh - 140px);
}
.col-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.col-main:fullscreen {
  background: var(--bg);
  padding: 10px;
}
.col-panel {
  width: 300px;
  flex: none;
  min-height: calc(100vh - 140px);
}

/* ============ 报价条 ============ */
.head-bar {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
  padding: 8px 14px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.hb-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.hb-code {
  font-family: var(--mono);
  font-size: 18px;
  font-weight: 700;
}
.hb-name {
  font-size: 18px;
  font-weight: 600;
}
.hb-market {
  font-size: 12px;
  color: var(--text-secondary);
  border: 1px solid var(--border-strong);
  border-radius: 4px;
  padding: 0 4px;
}
.hb-src-tag {
  font-size: 12px;
  color: var(--primary);
  background: var(--primary-weak);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 6px;
}
.hb-listing {
  font-size: 12px;
  color: var(--text-secondary);
  font-family: var(--mono);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 6px;
  white-space: nowrap;
}
.hb-price {
  display: flex;
  flex-direction: column;
  line-height: 1.1;
}
.hb-last {
  font-size: 26px;
  font-weight: 700;
  font-family: var(--mono);
}
.hb-chg {
  font-size: 13px;
  font-family: var(--mono);
}
.hb-grid {
  display: grid;
  grid-template-columns: repeat(4, auto);
  gap: 2px 18px;
  font-size: 12px;
}
.hb-grid label {
  color: var(--text-faint);
  margin-right: 4px;
}
.hb-grid span {
  font-family: var(--mono);
  color: var(--text);
}

/* ============ 工具栏 ============ */
.toolbar {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 10px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.tb-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.tb-row-second {
  border-top: 1px dashed var(--border);
  padding-top: 5px;
}
.tb-group {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}
.tb-label {
  font-size: 12px;
  color: var(--text-faint);
  margin-right: 2px;
}
.tb-spacer {
  flex: 1;
}
.tb-btn {
  padding: 3px 10px;
  font-size: 12px;
  border: 1px solid var(--border-strong);
  background: transparent;
  border-radius: 5px;
  cursor: pointer;
  color: var(--text);
  white-space: nowrap;
}
.tb-btn:hover {
  border-color: var(--primary);
  color: var(--primary);
}
.tb-btn.active {
  background: var(--primary);
  color: #fff;
  border-color: var(--primary);
}
.tb-btn-xs {
  padding: 2px 8px;
  font-size: 11px;
}
.tb-select {
  padding: 3px 8px;
  font-size: 12px;
}
.chk {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  cursor: pointer;
  color: var(--text);
  white-space: nowrap;
}
.chk input {
  accent-color: var(--primary);
}

/* ============ 图表区 ============ */
.charts-area {
  display: grid;
  gap: 8px;
}
.charts-area.one {
  grid-template-columns: 1fr;
}
.charts-area.two {
  grid-template-columns: 1fr 1fr;
}
.charts-area.four {
  grid-template-columns: 1fr 1fr;
}
.chart-cell {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  min-height: 320px;
  padding: 6px;
  display: flex;
  flex-direction: column;
  /* 面板开合时容器必须能收缩（否则 canvas 固定宽度把 grid item 撑住，
     面板重新打开后图表保持全宽溢出，ResizeObserver 不再触发） */
  min-width: 0;
  overflow: hidden;
}
.chart-state {
  display: flex;
  flex-direction: column;
  align-items: center;
}
.slot-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 4px 6px;
}
.slot-select {
  padding: 2px 8px;
  font-size: 12px;
  width: auto;
}
.slot-tag {
  font-size: 11px;
  color: var(--primary);
  background: var(--primary-weak);
  border-radius: 4px;
  padding: 0 5px;
}

/* ============ 键盘精灵 ============ */
.sprite-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.25);
  z-index: 400;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding-top: 12vh;
}
.sprite-box {
  width: 420px;
  max-width: 90vw;
  background: var(--panel);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.sprite-input {
  width: 100%;
  border: none;
  outline: none;
  padding: 12px 16px;
  font-size: 18px;
  font-family: var(--mono);
  background: transparent;
  color: var(--text);
  border-bottom: 1px solid var(--border);
}
.sprite-hint {
  padding: 10px 16px;
  font-size: 13px;
  color: var(--text-faint);
}
.sprite-list {
  list-style: none;
  margin: 0;
  padding: 4px 0;
  max-height: 320px;
  overflow-y: auto;
}
.sprite-option {
  display: flex;
  align-items: baseline;
  gap: 10px;
  width: 100%;
  padding: 7px 16px;
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 14px;
  color: var(--text);
  text-align: left;
}
.sprite-option:hover {
  background: var(--primary-weak);
}
.so-code {
  font-family: var(--mono);
  color: var(--primary);
  min-width: 64px;
}
.so-meta {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-faint);
}

/* ============ MA 参数弹窗 ============ */
.ma-modal {
  width: 420px;
}
.ma-modal h3 {
  margin: 0 0 14px;
  font-size: 16px;
}
.ma-hint {
  font-size: 12px;
  color: var(--text-faint);
  margin: 10px 0 0;
}
.ma-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}
</style>
