<script setup lang="ts">
/**
 * 策略库页 /strategies（契约 v1.3 草案 S1–S5，P0）。
 * - 列表（分页 + kind 过滤）+ 导出 JSON
 * - 新建（S2）/ 详情（S3）/ 删除（S4，确认；草案无 PUT，编辑=复制新建）
 * - 多策略组合回测（S5）：勾选多个 single 策略 → 资金 1/N → JobProgress → PortfolioResult（复用 PortfolioResultPanel）
 * 纯真接口模式：无 mock；无下单语义（NF-21 精神）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useAppStore } from '../stores/app';
import { useStrategiesStore } from '../stores/strategies';
import type {
  MultiStrategyRunRequest,
  MultiStrategySlot,
  SavedStrategy,
  SavedStrategyCreate,
  StrategyKind,
} from '../types';
import { fmtDate, fmtDateTime, fmtSignedPct } from '../utils/format';
import { toastError, toastSuccess, toastWarning } from '../utils/toast';
import ExportButton from '../components/ExportButton.vue';
import JobProgress from '../components/JobProgress.vue';
import Pagination from '../components/Pagination.vue';
import PortfolioResultPanel from '../components/PortfolioResultPanel.vue';
import StateBlock from '../components/StateBlock.vue';

const app = useAppStore();
const store = useStrategiesStore();

/* ---------- 列表 ---------- */
const kindFilter = ref<StrategyKind | ''>('');
const page = ref(1);
const pageSize = 20;

const KIND_TABS: { value: StrategyKind | ''; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'single', label: '单标的' },
  { value: 'portfolio', label: '组合' },
  { value: 'multi', label: '多策略' },
];

const KIND_LABEL: Record<StrategyKind, string> = {
  single: '单标的',
  portfolio: '组合',
  multi: '多策略',
};

/** 类型徽章样式（展示映射，不参与业务判断） */
function kindBadgeClass(kind: StrategyKind): string {
  if (kind === 'single') return 'badge-success';
  if (kind === 'portfolio') return 'badge-running';
  return 'badge-warning';
}

async function loadList(): Promise<void> {
  await store.loadList(kindFilter.value, page.value, pageSize);
}

function onKindChange(): void {
  page.value = 1;
  void loadList();
}

function onPageChange(p: number): void {
  page.value = p;
  void loadList();
}

function tagList(s: SavedStrategy): string[] {
  return Array.isArray(s.tags) ? s.tags : [];
}

/* ---------- 多策略组合回测（S5） ---------- */
const selectedIds = ref<Set<string>>(new Set());

/** 仅 single 且 context.symbol 可解析的策略可勾选（每槽位 1 个 code） */
function selectable(s: SavedStrategy): boolean {
  return s.kind === 'single' && typeof s.context?.symbol === 'string' && s.context.symbol.length > 0;
}

function selectableReason(s: SavedStrategy): string {
  if (s.kind !== 'single') return '仅单标的（single）策略可参与多策略组合回测';
  if (!s.context?.symbol) return '该策略未保存标的（context.symbol 为空）';
  return '';
}

function toggleSelect(id: string): void {
  const next = new Set(selectedIds.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selectedIds.value = next;
}

function isSelected(id: string): boolean {
  return selectedIds.value.has(id);
}

function toggleSelectAll(): void {
  const selectableItems = store.items.filter(selectable);
  const allSelected = selectableItems.length > 0 && selectableItems.every((s) => isSelected(s.id));
  const next = new Set(selectedIds.value);
  for (const s of selectableItems) {
    if (allSelected) next.delete(s.id);
    else next.add(s.id);
  }
  selectedIds.value = next;
}

function codeFromSymbol(symbol: string): string {
  const idx = symbol.indexOf(':');
  return idx >= 0 ? symbol.slice(idx + 1) : symbol;
}

const selectedStrategies = computed<SavedStrategy[]>(() =>
  store.items.filter((s) => selectedIds.value.has(s.id) && selectable(s)),
);

const runEnabled = computed(() => {
  const n = selectedStrategies.value.length;
  return n >= 1 && n <= 10;
});

/* 运行弹窗 */
const showRun = ref(false);
type RunPhase = 'config' | 'running' | 'result';
const runPhase = ref<RunPhase>('config');
const runCash = ref(1000000);
const runStart = ref('2020-01-01');
const runEnd = ref('2025-12-31');
const runCommission = ref(0.0003);
const runMinCommission = ref(5.0);
const runStampTax = ref(0.001);
const runSlippage = ref(0);
const runExecution = ref<'next_open' | 'next_close'>('next_open');

function openRun(): void {
  if (!runEnabled.value) {
    toastWarning(selectedStrategies.value.length === 0 ? '请先勾选至少 1 个单标的策略' : '组合槽位上限 10 个');
    return;
  }
  const first = selectedStrategies.value[0];
  const tc = first.trade_config ?? {};
  const ctx = first.context ?? {};
  runCash.value = typeof tc.cash === 'number' ? tc.cash : 1000000;
  runCommission.value = typeof tc.commission === 'number' ? tc.commission : 0.0003;
  runMinCommission.value = typeof tc.min_commission === 'number' ? tc.min_commission : 5.0;
  runStampTax.value = typeof tc.stamp_tax === 'number' ? tc.stamp_tax : 0.001;
  runSlippage.value = typeof tc.slippage === 'number' ? tc.slippage : 0;
  runExecution.value = tc.execution === 'next_close' ? 'next_close' : 'next_open';
  runStart.value = typeof ctx.start_date === 'string' && ctx.start_date ? ctx.start_date : '2020-01-01';
  runEnd.value = typeof ctx.end_date === 'string' && ctx.end_date ? ctx.end_date : '2025-12-31';
  store.resetRun();
  runPhase.value = 'config';
  showRun.value = true;
}

function buildMultiRequest(): MultiStrategyRunRequest | null {
  const slots: MultiStrategySlot[] = selectedStrategies.value.map((s) => {
    const symbol = typeof s.context?.symbol === 'string' ? s.context.symbol : '';
    return {
      strategy: s.strategy,
      label: s.strategy_label || s.name,
      code: codeFromSymbol(symbol),
      params: { ...((s.params ?? {}) as Record<string, string | number | boolean>) },
    };
  });
  if (slots.length === 0) return null;
  return {
    market: app.market,
    items: slots,
    cash: Number(runCash.value),
    commission: Number(runCommission.value),
    min_commission: Number(runMinCommission.value),
    stamp_tax: Number(runStampTax.value),
    slippage: Number(runSlippage.value),
    execution: runExecution.value,
    start: runStart.value,
    end: runEnd.value,
  };
}

async function startRun(): Promise<void> {
  if (!(runCash.value > 0)) {
    toastWarning('总资金必须为正数');
    return;
  }
  if (!runStart.value || !runEnd.value || runStart.value > runEnd.value) {
    toastWarning('请检查起止日期');
    return;
  }
  const req = buildMultiRequest();
  if (!req) {
    toastWarning('勾选策略无可回测标的');
    return;
  }
  try {
    await store.runMulti(req);
    runPhase.value = 'running';
    toastSuccess(`多策略组合回测已提交：${req.items.length} 个策略槽位`);
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

function closeRun(): void {
  store.resetRun();
  showRun.value = false;
}

/* ---------- 新建 / 复制新建（S2；草案无 PUT，编辑=删除+重建，前端以"复制新建"承载） ---------- */
const showCreate = ref(false);
const createError = ref('');
const createName = ref('');
const createKind = ref<StrategyKind>('single');
const createStrategy = ref('ma_cross');
const createParamsJson = ref('{}');
const createContextJson = ref('{}');
const createTradeConfigJson = ref('{}');
const createTagsText = ref('');
const createNotes = ref('');

const CONTEXT_PLACEHOLDER: Record<StrategyKind, string> = {
  single: '{"symbol":"SH:600519","start_date":"2020-01-01","end_date":"2025-12-31"}',
  portfolio: '{"stocks":["600519","000001"],"start_date":"2020-01-01","end_date":"2025-12-31"}',
  multi: '{"items":[{"strategy":"ma_cross","label":"双均线交叉","code":"600519","params":{"fast":5,"slow":20}}]}',
};

function schemaByName(name: string) {
  return store.schemas.find((s) => s.name === name);
}

function resetCreateForm(): void {
  createError.value = '';
  createName.value = '';
  createKind.value = 'single';
  createStrategy.value = store.schemas[0]?.name ?? 'ma_cross';
  createParamsJson.value = defaultsJson(createStrategy.value);
  createContextJson.value = CONTEXT_PLACEHOLDER.single;
  createTradeConfigJson.value = '{"cash":1000000,"commission":0.0003,"min_commission":5.0,"stamp_tax":0.001,"slippage":0,"execution":"next_open"}';
  createTagsText.value = '';
  createNotes.value = '';
}

function defaultsJson(strategyName: string): string {
  const schema = schemaByName(strategyName);
  if (!schema) return '{}';
  const defaults: Record<string, string | number | boolean> = {};
  for (const p of schema.params) {
    const d = p.default;
    defaults[p.name] = d === null || d === undefined ? '' : (d as string | number | boolean);
  }
  return JSON.stringify(defaults, null, 2);
}

function openCreate(): void {
  resetCreateForm();
  showCreate.value = true;
}

function onFormStrategyChange(): void {
  createParamsJson.value = defaultsJson(createStrategy.value);
}

function onFormKindChange(): void {
  createContextJson.value = CONTEXT_PLACEHOLDER[createKind.value];
}

function parseJsonField(text: string, fieldName: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (e) {
    throw new Error(`${fieldName} 不是合法 JSON：${(e as Error).message}`);
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${fieldName} 必须是 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

async function saveCreate(): Promise<void> {
  createError.value = '';
  const name = createName.value.trim();
  if (!name) {
    createError.value = '请填写策略名称';
    return;
  }
  if (name.length > 120) {
    createError.value = '策略名称不能超过 120 字符';
    return;
  }
  let params: Record<string, unknown>;
  let context: Record<string, unknown>;
  let tradeConfig: Record<string, unknown>;
  try {
    params = parseJsonField(createParamsJson.value, '参数');
    context = parseJsonField(createContextJson.value, '上下文');
    tradeConfig = parseJsonField(createTradeConfigJson.value, '交易配置');
  } catch (e) {
    createError.value = e instanceof Error ? e.message : String(e);
    return;
  }
  const tags = createTagsText.value
    .split(/[,，]/)
    .map((t) => t.trim())
    .filter(Boolean);
  const req: SavedStrategyCreate = {
    name,
    kind: createKind.value,
    strategy: createKind.value === 'multi' ? 'multi' : createStrategy.value,
    strategy_label: schemaByName(createStrategy.value)?.label ?? '',
    params: params as Record<string, string | number | boolean>,
    context,
    trade_config: tradeConfig,
    snapshot: {},
    tags,
    notes: createNotes.value.trim(),
  };
  try {
    const saved = await store.create(req);
    toastSuccess(`已保存策略：${saved.name}`);
    showCreate.value = false;
    await loadList();
  } catch (e) {
    createError.value = e instanceof Error ? e.message : String(e);
  }
}

/** 复制新建（编辑替代：草案 S4 无 PUT，改名/改参=复制为新建） */
function openClone(s: SavedStrategy): void {
  createError.value = '';
  createName.value = `${s.name}（副本）`;
  createKind.value = s.kind;
  createStrategy.value = s.strategy === 'multi' ? (store.schemas[0]?.name ?? 'ma_cross') : s.strategy;
  createParamsJson.value = JSON.stringify(s.params ?? {}, null, 2);
  createContextJson.value = JSON.stringify(s.context ?? {}, null, 2);
  createTradeConfigJson.value = JSON.stringify(s.trade_config ?? {}, null, 2);
  createTagsText.value = tagList(s).join(', ');
  createNotes.value = s.notes ?? '';
  showCreate.value = true;
}

/* ---------- 详情（S3） ---------- */
const showDetail = ref(false);
const detailId = ref('');
const detailLoading = ref(false);
const detailError = ref('');

async function openDetail(s: SavedStrategy): Promise<void> {
  detailId.value = s.id;
  showDetail.value = true;
  detailLoading.value = true;
  detailError.value = '';
  const data = await store.loadDetail(s.id);
  detailLoading.value = false;
  if (!data) {
    detailError.value = store.detailError || '加载策略详情失败';
  }
}

function closeDetail(): void {
  showDetail.value = false;
  store.clearDetail();
}

const detail = computed(() => store.detail);

/* ---------- 删除（S4，确认） ---------- */
async function onDelete(s: SavedStrategy): Promise<void> {
  const ok = window.confirm(`确认删除策略「${s.name}」？此操作不可恢复。`);
  if (!ok) return;
  try {
    await store.remove(s.id);
    selectedIds.value.delete(s.id);
    toastSuccess(`已删除策略：${s.name}`);
    if (store.items.length === 0 && page.value > 1) {
      page.value -= 1;
    }
    await loadList();
  } catch {
    // 错误 toast 已由 store.remove 统一弹出
  }
}

/* ---------- 导出 JSON（策略列表） ---------- */
async function exportList(): Promise<{ blob: Blob; filename: string }> {
  const payload = {
    items: store.items,
    page: store.page,
    page_size: store.pageSize,
    total: store.total,
    total_pages: store.totalPages,
  };
  return {
    blob: new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json; charset=utf-8' }),
    filename: `strategies_${new Date().toISOString().slice(0, 10)}.json`,
  };
}

onMounted(() => {
  void store.loadSchemas().then(() => {
    if (createStrategy.value === 'ma_cross' && store.schemas[0]) createStrategy.value = store.schemas[0].name;
  });
  void loadList();
});

onBeforeUnmount(() => {
  store.stopPolling();
});
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h2 class="page-title">策略库</h2>
      <div class="header-actions">
        <ExportButton :fetcher="exportList" label="导出JSON" />
        <button class="btn btn-primary" :disabled="!runEnabled" @click="openRun">
          组合回测（{{ selectedStrategies.length }}）
        </button>
        <button class="btn" @click="openCreate">新建策略</button>
      </div>
    </div>

    <!-- kind 过滤 -->
    <div class="tabs">
      <button
        v-for="t in KIND_TABS"
        :key="t.value"
        class="tab-btn"
        :class="{ active: kindFilter === t.value }"
        @click="kindFilter = t.value; onKindChange()"
      >
        {{ t.label }}
      </button>
    </div>

    <div v-if="store.listError" class="error-banner">⚠ {{ store.listError }}</div>
    <StateBlock v-else-if="store.loading && store.items.length === 0" state="loading" />
    <StateBlock
      v-else-if="store.items.length === 0"
      state="empty"
      :message="app.dataLakeEmpty ? '数据湖为空：策略依赖回测结果，请先同步行情数据（见顶部引导条）。' : '暂无策略，点击「新建策略」创建'"
    />

    <template v-else>
      <div class="panel table-panel">
        <div class="table-wrap">
          <table class="tbl">
            <thead>
              <tr>
                <th style="width: 36px">
                  <input
                    type="checkbox"
                    :checked="store.items.length > 0 && store.items.filter(selectable).every((s) => isSelected(s.id))"
                    @change="toggleSelectAll"
                  />
                </th>
                <th>名称</th>
                <th>类型</th>
                <th>策略</th>
                <th>标签</th>
                <th>创建时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in store.items" :key="s.id">
                <td>
                  <input
                    type="checkbox"
                    :checked="isSelected(s.id)"
                    :disabled="!selectable(s)"
                    :title="selectable(s) ? '' : selectableReason(s)"
                    @change="toggleSelect(s.id)"
                  />
                </td>
                <td>
                  <div class="name-cell">
                    <span class="strategy-name">{{ s.name }}</span>
                    <span v-if="s.snapshot?.total_return !== undefined" class="name-snapshot" :class="(s.snapshot.total_return ?? 0) >= 0 ? 'text-green' : 'text-red'">
                      {{ fmtSignedPct((s.snapshot.total_return as number | null) ?? null) }}
                    </span>
                  </div>
                </td>
                <td><span class="badge" :class="kindBadgeClass(s.kind)">{{ KIND_LABEL[s.kind] }}</span></td>
                <td class="mono-cell">{{ s.strategy }}</td>
                <td>
                  <span v-if="tagList(s).length" class="tag-row">
                    <span v-for="t in tagList(s)" :key="t" class="tag-chip">{{ t }}</span>
                  </span>
                  <span v-else class="text-faint">-</span>
                </td>
                <td class="mono-cell">{{ fmtDate(s.created_at?.slice?.(0, 10)) }}</td>
                <td>
                  <div class="row-actions">
                    <button class="btn btn-ghost btn-xs" @click="openDetail(s)">查看</button>
                    <button class="btn btn-ghost btn-xs" @click="openClone(s)">复制</button>
                    <button class="btn btn-danger btn-xs" :disabled="store.deletingId === s.id" @click="onDelete(s)">
                      {{ store.deletingId === s.id ? '删除中…' : '删除' }}
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <Pagination
          :page="store.page"
          :page-size="store.pageSize"
          :total="store.total"
          :total-pages="store.totalPages"
          @change="onPageChange"
        />
      </div>
    </template>

    <!-- 新建 / 复制新建 弹窗 -->
    <div v-if="showCreate" class="modal-mask" @click.self="showCreate = false">
      <div class="modal">
        <div class="modal-title">
          <h3>{{ createName.includes('（副本）') ? '复制新建策略' : '新建策略' }}</h3>
          <button class="btn btn-ghost btn-xs" @click="showCreate = false">关闭</button>
        </div>
        <div v-if="store.schemasError" class="error-banner">⚠ 策略注册表加载失败：{{ store.schemasError }}</div>

        <div class="form-grid">
          <label class="field">
            <span>名称（必填）</span>
            <input v-model="createName" class="input" type="text" maxlength="120" placeholder="如 双均线-茅台" />
          </label>
          <label class="field">
            <span>类型</span>
            <select v-model="createKind" class="input" @change="onFormKindChange">
              <option value="single">单标的</option>
              <option value="portfolio">组合（1 策略 × N 标的）</option>
              <option value="multi">多策略组合</option>
            </select>
          </label>
          <label v-if="createKind !== 'multi'" class="field">
            <span>策略</span>
            <select v-model="createStrategy" class="input" @change="onFormStrategyChange">
              <option v-for="sc in store.schemas" :key="sc.name" :value="sc.name">{{ sc.label }}（{{ sc.name }}）</option>
            </select>
          </label>
          <div v-if="createKind !== 'multi'" class="field">
            <span>参数（JSON）</span>
            <textarea v-model="createParamsJson" class="input code-textarea" rows="4" spellcheck="false"></textarea>
          </div>
          <div class="field field-wide">
            <span>上下文 context（JSON，随类型变化）</span>
            <textarea v-model="createContextJson" class="input code-textarea" rows="4" spellcheck="false" :placeholder="CONTEXT_PLACEHOLDER[createKind]"></textarea>
          </div>
          <div class="field field-wide">
            <span>交易配置 trade_config（JSON）</span>
            <textarea v-model="createTradeConfigJson" class="input code-textarea" rows="3" spellcheck="false"></textarea>
          </div>
          <label class="field">
            <span>标签（逗号分隔）</span>
            <input v-model="createTagsText" class="input" type="text" placeholder="如 优选, 低回撤" />
          </label>
          <label class="field field-wide">
            <span>备注</span>
            <input v-model="createNotes" class="input" type="text" placeholder="可选" />
          </label>
        </div>

        <p v-if="createError" class="form-error">⚠ {{ createError }}</p>
        <div class="modal-actions">
          <button class="btn" @click="showCreate = false">取消</button>
          <button class="btn btn-primary" :disabled="store.creating" @click="saveCreate">
            {{ store.creating ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 详情弹窗（S3） -->
    <div v-if="showDetail" class="modal-mask" @click.self="closeDetail">
      <div class="modal">
        <div class="modal-title">
          <h3>策略详情</h3>
          <button class="btn btn-ghost btn-xs" @click="closeDetail">关闭</button>
        </div>
        <StateBlock v-if="detailLoading" state="loading" />
        <div v-else-if="detailError" class="error-banner">⚠ {{ detailError }}</div>
        <div v-else-if="detail" class="detail-body">
          <div class="detail-row">
            <span class="detail-label">名称</span><span class="detail-value">{{ detail.name }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">类型</span>
            <span class="detail-value"><span class="badge" :class="kindBadgeClass(detail.kind)">{{ KIND_LABEL[detail.kind] }}</span></span>
          </div>
          <div class="detail-row">
            <span class="detail-label">策略</span><span class="detail-value mono-cell">{{ detail.strategy }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">参数</span><span class="detail-value code-block">{{ JSON.stringify(detail.params) }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">上下文</span><span class="detail-value code-block">{{ JSON.stringify(detail.context) }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">交易配置</span><span class="detail-value code-block">{{ JSON.stringify(detail.trade_config) }}</span>
          </div>
          <div v-if="Object.keys(detail.snapshot ?? {}).length" class="detail-row">
            <span class="detail-label">成绩快照</span><span class="detail-value code-block">{{ JSON.stringify(detail.snapshot) }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">标签</span>
            <span class="detail-value">
              <span v-if="tagList(detail).length" class="tag-row">
                <span v-for="t in tagList(detail)" :key="t" class="tag-chip">{{ t }}</span>
              </span>
              <span v-else>-</span>
            </span>
          </div>
          <div v-if="detail.notes" class="detail-row">
            <span class="detail-label">备注</span><span class="detail-value">{{ detail.notes }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">创建</span><span class="detail-value mono-cell">{{ fmtDateTime(detail.created_at) }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">更新</span><span class="detail-value mono-cell">{{ fmtDateTime(detail.updated_at) }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">版本</span><span class="detail-value mono-cell">{{ detail.app_version }}</span>
          </div>
        </div>
        <div class="modal-actions" v-if="detail">
          <button class="btn" @click="closeDetail">关闭</button>
          <button class="btn btn-primary" @click="closeDetail(); openClone(detail)">复制新建</button>
        </div>
      </div>
    </div>

    <!-- 多策略组合回测弹窗（S5） -->
    <div v-if="showRun" class="modal-mask" @click.self="closeRun">
      <div class="modal modal-wide">
        <div class="modal-title">
          <h3>多策略组合回测（资金 1/N 均分）</h3>
          <button class="btn btn-ghost btn-xs" @click="closeRun">关闭</button>
        </div>

        <!-- phase: config -->
        <div v-if="runPhase === 'config'">
          <div class="run-slots">
            <div v-for="s in selectedStrategies" :key="s.id" class="run-slot">
              <span class="run-slot-label">{{ s.strategy_label || s.name }}</span>
              <span class="run-slot-code mono-cell">{{ codeFromSymbol(String(s.context?.symbol ?? '')) }}</span>
              <span class="run-slot-params mono-cell">{{ JSON.stringify(s.params ?? {}) }}</span>
            </div>
          </div>
          <div class="form-grid">
            <label class="field">
              <span>总资金（1/N 到各槽位）</span>
              <input v-model.number="runCash" class="input" type="number" min="1000" step="10000" />
            </label>
            <label class="field">
              <span>起始</span>
              <input v-model="runStart" class="input" type="date" />
            </label>
            <label class="field">
              <span>结束</span>
              <input v-model="runEnd" class="input" type="date" />
            </label>
            <label class="field">
              <span>佣金率</span>
              <input v-model.number="runCommission" class="input" type="number" min="0" step="0.0001" />
            </label>
            <label class="field">
              <span>最低佣金</span>
              <input v-model.number="runMinCommission" class="input" type="number" min="0" step="0.1" />
            </label>
            <label class="field">
              <span>印花税</span>
              <input v-model.number="runStampTax" class="input" type="number" min="0" step="0.0001" />
            </label>
            <label class="field">
              <span>滑点</span>
              <input v-model.number="runSlippage" class="input" type="number" min="0" step="0.001" />
            </label>
            <label class="field">
              <span>成交价</span>
              <select v-model="runExecution" class="input">
                <option value="next_open">开盘价</option>
                <option value="next_close">收盘价</option>
              </select>
            </label>
          </div>
          <div class="modal-actions">
            <button class="btn" @click="closeRun">取消</button>
            <button class="btn btn-primary" :disabled="store.running" @click="startRun">
              {{ store.running ? '提交中…' : `开始组合回测（${selectedStrategies.length} 槽位）` }}
            </button>
          </div>
        </div>

        <!-- phase: running -->
        <div v-else-if="runPhase === 'running'">
          <JobProgress v-if="store.job" :job="store.job" />
          <div v-if="store.resultError" class="error-banner">⚠ {{ store.resultError }}</div>
        </div>

        <!-- phase: result -->
        <div v-else-if="runPhase === 'result'">
          <PortfolioResultPanel v-if="store.result" :result="store.result" title="多策略组合回测结果（资金 1/N）" />
          <div v-else-if="store.resultError" class="error-banner">⚠ {{ store.resultError }}</div>
          <div class="modal-actions">
            <button class="btn btn-primary" @click="closeRun">完成</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.header-actions {
  display: flex;
  gap: 8px;
  align-items: center;
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
.table-panel {
  padding: 10px 14px;
}
.name-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.strategy-name {
  font-weight: 600;
}
.name-snapshot {
  font-size: 12px;
  font-family: var(--mono);
}
.tag-row {
  display: inline-flex;
  gap: 4px;
  flex-wrap: wrap;
}
.tag-chip {
  background: var(--primary-weak);
  color: var(--primary);
  border-radius: 999px;
  padding: 1px 8px;
  font-size: 11px;
}
.text-faint {
  color: var(--text-faint);
}
.mono-cell {
  font-family: var(--mono);
}
.row-actions {
  display: flex;
  gap: 4px;
}
.modal-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.modal-title h3 {
  margin: 0;
  font-size: 16px;
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.field-wide {
  grid-column: 1 / -1;
}
.code-textarea {
  font-family: var(--mono);
  font-size: 12px;
  resize: vertical;
}
.form-error {
  color: var(--red);
  font-size: 13px;
  margin: 10px 0 0;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
}
.modal-wide {
  width: 860px;
}
.detail-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.detail-row {
  display: flex;
  gap: 10px;
  align-items: baseline;
  font-size: 13px;
}
.detail-label {
  flex: none;
  width: 90px;
  color: var(--text-secondary);
  font-size: 12px;
}
.detail-value {
  flex: 1;
  min-width: 0;
  word-break: break-all;
}
.code-block {
  font-family: var(--mono);
  font-size: 12px;
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 4px 8px;
}
.run-slots {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
}
.run-slot {
  display: flex;
  gap: 10px;
  align-items: baseline;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
}
.run-slot-label {
  font-weight: 600;
}
.run-slot-code {
  color: var(--primary);
}
.run-slot-params {
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
