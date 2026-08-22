<script setup lang="ts">
/**
 * 自选股侧栏（StockWatchlistSidebar）：通达信风格左侧栏。
 * - 分组管理（localStorage 持久化；首次使用自动从监控自选导入）
 * - 每行：名称 + 代码 + 实时价 + 涨跌幅（批量报价 10s 轮询）
 * - 搜索添加 / 单行删除 / 组内拖拽排序 / 点击跳转详情页
 * 报价来自 /stock/quotes（tdx 在线直连，单批 ≤ 80 只）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { api } from '../api';
import type { StockQuoteLite } from '../types';
import SecuritySearchBox from './SecuritySearchBox.vue';

const props = withDefaults(
  defineProps<{
    /** 当前标的代码（行高亮） */
    activeCode?: string;
    market?: string;
  }>(),
  { activeCode: '', market: 'CN' },
);

const router = useRouter();

const LS_GROUPS = 'kuantix.stock.watchGroups';
const LS_NAMES = 'kuantix.stock.watchNames';
const POLL_MS = 10_000;

interface WatchGroup {
  name: string;
  codes: string[];
}

const groups = ref<WatchGroup[]>([]);
const names = ref<Record<string, string>>({});
const activeGroup = ref(0);
const quotes = ref<Record<string, StockQuoteLite>>({});
let pollTimer: number | null = null;
let seeded = false;

function load(): void {
  try {
    const rawGroups = localStorage.getItem(LS_GROUPS);
    if (rawGroups) {
      const parsed = JSON.parse(rawGroups) as WatchGroup[];
      if (Array.isArray(parsed) && parsed.length > 0) groups.value = parsed;
    }
    const rawNames = localStorage.getItem(LS_NAMES);
    if (rawNames) names.value = JSON.parse(rawNames) as Record<string, string>;
  } catch {
    /* 损坏的本地数据直接忽略，走默认空分组 */
  }
  if (groups.value.length === 0) groups.value = [{ name: '自选', codes: [] }];
}

function save(): void {
  localStorage.setItem(LS_GROUPS, JSON.stringify(groups.value));
  localStorage.setItem(LS_NAMES, JSON.stringify(names.value));
}

/** 首次使用（本地无自选）时从监控 watchlist 导入做初始分组 */
async function seedFromMonitor(): Promise<void> {
  if (seeded || groups.value.some((g) => g.codes.length > 0)) return;
  seeded = true;
  try {
    const env = await api.getWatchlist(props.market, 1, 80);
    const items = env.data.items ?? [];
    if (items.length > 0) {
      groups.value[0].codes = items.map((i) => i.code);
      for (const i of items) names.value[i.code] = i.name;
      save();
      void refreshQuotes();
    }
  } catch {
    /* 监控接口不可用时保持空分组，不打扰用户 */
  }
}

const currentGroup = computed<WatchGroup | undefined>(() => groups.value[activeGroup.value]);

async function refreshQuotes(): Promise<void> {
  const codes = [...new Set(groups.value.flatMap((g) => g.codes))].slice(0, 80);
  if (codes.length === 0) {
    quotes.value = {};
    return;
  }
  try {
    const env = await api.getStockQuotes(codes, { market: props.market });
    const map: Record<string, StockQuoteLite> = {};
    for (const q of env.data.items ?? []) map[q.code] = q;
    quotes.value = map;
  } catch {
    /* 轮询静默失败：保留上次报价 */
  }
}

function addStock(hit: { code: string; name: string }): void {
  const g = currentGroup.value;
  if (!g) return;
  if (!g.codes.includes(hit.code)) {
    g.codes.push(hit.code);
    names.value[hit.code] = hit.name;
    save();
    void refreshQuotes();
  }
}

function removeStock(code: string): void {
  const g = currentGroup.value;
  if (!g) return;
  g.codes = g.codes.filter((c) => c !== code);
  save();
}

function gotoStock(code: string): void {
  router.push({ path: `/stock/${code}`, query: { name: names.value[code] || '' } });
}

function addGroup(): void {
  const name = window.prompt('新分组名称：')?.trim();
  if (!name) return;
  if (groups.value.some((g) => g.name === name)) return;
  groups.value.push({ name, codes: [] });
  activeGroup.value = groups.value.length - 1;
  save();
}

function removeGroup(idx: number): void {
  if (groups.value.length <= 1) return;
  const g = groups.value[idx];
  if (g.codes.length > 0 && !window.confirm(`删除分组「${g.name}」及其 ${g.codes.length} 只股票？`)) return;
  groups.value.splice(idx, 1);
  activeGroup.value = Math.min(activeGroup.value, groups.value.length - 1);
  save();
}

/* 组内拖拽排序 */
let dragIdx = -1;

function onDragStart(idx: number): void {
  dragIdx = idx;
}

function onDrop(idx: number): void {
  const g = currentGroup.value;
  if (!g || dragIdx < 0 || dragIdx === idx) return;
  const [moved] = g.codes.splice(dragIdx, 1);
  g.codes.splice(idx, 0, moved);
  dragIdx = -1;
  save();
}

function pctOf(code: string): { text: string; up: boolean } {
  const q = quotes.value[code];
  if (!q || q.change_pct == null) return { text: '--', up: false };
  const p = q.change_pct * 100;
  return { text: `${p >= 0 ? '+' : ''}${p.toFixed(2)}%`, up: p >= 0 };
}

onMounted(() => {
  load();
  void seedFromMonitor();
  void refreshQuotes();
  pollTimer = window.setInterval(() => void refreshQuotes(), POLL_MS);
});

onBeforeUnmount(() => {
  if (pollTimer !== null) window.clearInterval(pollTimer);
});
</script>

<template>
  <div class="watch-sidebar">
    <div class="watch-head">
      <span class="watch-title">自选股</span>
      <span class="watch-count">{{ currentGroup?.codes.length ?? 0 }}</span>
    </div>

    <div class="watch-search">
      <SecuritySearchBox :market="market" placeholder="搜索代码/名称添加" @select="addStock" />
    </div>

    <div class="watch-tabs">
      <button
        v-for="(g, i) in groups"
        :key="g.name"
        type="button"
        class="watch-tab"
        :class="{ active: i === activeGroup }"
        :title="g.name"
        @click="activeGroup = i"
      >
        {{ g.name }}
      </button>
      <button type="button" class="watch-tab-add" title="新建分组" @click="addGroup">+</button>
      <button
        v-if="groups.length > 1"
        type="button"
        class="watch-tab-del"
        title="删除当前分组"
        @click="removeGroup(activeGroup)"
      >
        −
      </button>
    </div>

    <div class="watch-list">
      <div v-if="!currentGroup || currentGroup.codes.length === 0" class="watch-empty">
        暂无自选，用上方搜索框添加
      </div>
      <div
        v-for="(code, i) in currentGroup?.codes ?? []"
        :key="code"
        class="watch-row"
        :class="{ active: code === activeCode }"
        draggable="true"
        @dragstart="onDragStart(i)"
        @dragover.prevent
        @drop="onDrop(i)"
      >
        <button type="button" class="watch-main" @click="gotoStock(code)">
          <span class="wr-name" :title="names[code] || code">{{ names[code] || '—' }}</span>
          <span class="wr-code">{{ code }}</span>
          <span class="wr-quote">
            <span class="wr-price">{{ quotes[code]?.price?.toFixed(2) ?? '--' }}</span>
            <span class="wr-pct" :class="pctOf(code).up ? 'rise' : 'fall'">{{ pctOf(code).text }}</span>
          </span>
        </button>
        <button type="button" class="wr-remove" title="移出自选" @click.stop="removeStock(code)">×</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.watch-sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.watch-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 12px 6px;
}
.watch-title {
  font-weight: 600;
  font-size: 14px;
}
.watch-count {
  font-size: 11px;
  color: var(--text-faint);
  background: var(--primary-weak);
  color: var(--primary);
  border-radius: 999px;
  padding: 0 8px;
}
.watch-search {
  padding: 0 10px 8px;
}
.watch-search :deep(.security-search) {
  min-width: 0;
}
.watch-tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 0 8px 8px;
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
}
.watch-tab {
  border: none;
  background: transparent;
  padding: 4px 10px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: 6px;
  white-space: nowrap;
}
.watch-tab.active {
  background: var(--primary-weak);
  color: var(--primary);
  font-weight: 600;
}
.watch-tab-add,
.watch-tab-del {
  border: 1px dashed var(--border-strong);
  background: transparent;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  color: var(--text-faint);
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  flex: none;
}
.watch-tab-add:hover,
.watch-tab-del:hover {
  color: var(--primary);
  border-color: var(--primary);
}
.watch-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}
.watch-empty {
  padding: 24px 14px;
  text-align: center;
  color: var(--text-faint);
  font-size: 12px;
}
.watch-row {
  display: flex;
  align-items: stretch;
  border-bottom: 1px solid var(--border);
  cursor: grab;
}
.watch-row.active {
  background: var(--primary-weak);
}
.watch-main {
  flex: 1;
  min-width: 0;
  display: grid;
  grid-template-columns: 1fr auto;
  grid-template-rows: auto auto;
  column-gap: 6px;
  align-items: center;
  padding: 6px 6px 6px 12px;
  border: none;
  background: transparent;
  cursor: pointer;
  text-align: left;
}
.wr-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wr-code {
  grid-row: 2;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-faint);
}
.wr-quote {
  grid-row: 1 / 3;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 1px;
  font-variant-numeric: tabular-nums;
}
.wr-price {
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.wr-pct {
  font-family: var(--mono);
  font-size: 11px;
}
.wr-pct.rise {
  color: var(--red);
}
.wr-pct.fall {
  color: var(--green);
}
.wr-remove {
  flex: none;
  width: 22px;
  border: none;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  font-size: 14px;
  opacity: 0;
  transition: opacity 0.12s;
}
.watch-row:hover .wr-remove {
  opacity: 1;
}
.wr-remove:hover {
  color: var(--red);
}
</style>
