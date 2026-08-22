<script setup lang="ts">
/**
 * 证券搜索输入框（D8）：支持代码（精确/前缀）与名称（模糊），
 * 下拉展示「代码 名称 (交易所/类型)」供确认选择；选中后回填 code。
 * 供监控自选添加 / 选股标的池 / 回测标的池复用。
 */
import { onBeforeUnmount, ref, watch } from 'vue';
import { api } from '../api';
import type { SecurityHit } from '../types/data';

const props = withDefaults(
  defineProps<{
    /** 市场码（P0 仅 CN） */
    market?: string;
    /** 占位提示 */
    placeholder?: string;
    /** 延迟毫秒（防抖） */
    debounceMs?: number;
    /** 搜索条数上限 */
    limit?: number;
  }>(),
  { market: 'CN', placeholder: '输入代码或名称搜索，如 600000 / 浦发', debounceMs: 300, limit: 20 },
);

const emit = defineEmits<{
  (e: 'select', hit: SecurityHit): void;
  (e: 'clear'): void;
}>();

const text = ref('');
const hits = ref<SecurityHit[]>([]);
const open = ref(false);
const searching = ref(false);
const errorMsg = ref('');
let timer: number | null = null;

/** 交易所前缀 → 中文（纯展示映射，不做业务兜底） */
function exchangeLabel(exchange: string): string {
  if (exchange === 'sh') return '沪';
  if (exchange === 'sz') return '深';
  if (exchange === 'bj') return '北';
  return exchange.toUpperCase();
}

/** 证券类型 → 展示名（纯展示映射） */
function typeLabel(securityType: string): string {
  if (securityType.includes('A_STOCK')) return 'A股';
  if (securityType.includes('ETF')) return 'ETF';
  if (securityType.includes('INDEX')) return '指数';
  if (securityType.includes('BOND')) return '债券';
  if (securityType.includes('FUND')) return '基金';
  return securityType;
}

async function doSearch(): Promise<void> {
  const q = text.value.trim();
  if (!q) {
    hits.value = [];
    open.value = false;
    return;
  }
  searching.value = true;
  errorMsg.value = '';
  try {
    const env = await api.searchSecurities(q, props.market, props.limit);
    hits.value = env.data.items;
    open.value = true;
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e);
    hits.value = [];
    open.value = false;
  } finally {
    searching.value = false;
  }
}

watch(text, () => {
  if (timer !== null) window.clearTimeout(timer);
  timer = window.setTimeout(() => {
    void doSearch();
  }, props.debounceMs);
});

function pick(hit: SecurityHit): void {
  text.value = `${hit.code} ${hit.name}`;
  open.value = false;
  emit('select', hit);
}

function onBlur(): void {
  // 延迟关闭下拉，保证点击选项事件先触发
  window.setTimeout(() => {
    open.value = false;
  }, 150);
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') open.value = false;
  if (e.key === 'Enter') {
    e.preventDefault();
    if (hits.value.length > 0) pick(hits.value[0]);
  }
}

onBeforeUnmount(() => {
  if (timer !== null) window.clearTimeout(timer);
});
</script>

<template>
  <div class="security-search">
    <div class="search-input-wrap">
      <input
        v-model="text"
        class="input search-input"
        type="text"
        :placeholder="placeholder"
        autocomplete="off"
        @keydown="onKeydown"
        @focus="open = hits.length > 0"
        @blur="onBlur"
      />
      <span v-if="searching" class="search-spinner" title="搜索中…">…</span>
    </div>

    <div v-if="errorMsg" class="search-error" :title="errorMsg">搜索失败：{{ errorMsg }}</div>

    <ul v-if="open && hits.length > 0" class="search-dropdown">
      <li v-for="hit in hits" :key="hit.exchange + ':' + hit.code">
        <button type="button" class="search-option" @mousedown.prevent="pick(hit)">
          <span class="opt-code">{{ hit.code }}</span>
          <span class="opt-name">{{ hit.name }}</span>
          <span class="opt-meta">{{ exchangeLabel(hit.exchange) }} / {{ typeLabel(hit.security_type) }}</span>
        </button>
      </li>
    </ul>
    <div v-else-if="open && !searching && text.trim()" class="search-empty">无匹配证券</div>
  </div>
</template>

<style scoped>
.security-search {
  position: relative;
  min-width: 220px;
}
.search-input-wrap {
  position: relative;
}
.search-input {
  width: 100%;
  padding-right: 28px;
}
.search-spinner {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-dim, #888);
  font-size: 14px;
}
.search-error {
  margin-top: 4px;
  font-size: 12px;
  color: var(--up, #ef4146);
}
.search-dropdown {
  position: absolute;
  left: 0;
  right: 0;
  top: calc(100% + 4px);
  z-index: 30;
  max-height: 280px;
  overflow-y: auto;
  list-style: none;
  margin: 0;
  padding: 4px 0;
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border, #ddd);
  border-radius: 6px;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.12);
}
.search-option {
  display: flex;
  align-items: baseline;
  gap: 8px;
  width: 100%;
  padding: 7px 10px;
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 13px;
  color: var(--text, #222);
  text-align: left;
}
.search-option:hover {
  background: var(--accent-soft, #eef4ff);
}
.opt-code {
  font-family: var(--font-mono, monospace);
  color: var(--accent, #2f6fed);
  min-width: 64px;
}
.opt-name {
  font-weight: 500;
}
.opt-meta {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-dim, #888);
  white-space: nowrap;
}
.search-empty {
  position: absolute;
  left: 0;
  right: 0;
  top: calc(100% + 4px);
  z-index: 30;
  padding: 8px 10px;
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border, #ddd);
  border-radius: 6px;
  font-size: 12px;
  color: var(--text-dim, #888);
}
</style>
