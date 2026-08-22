<script setup lang="ts">
/** 监控看板页 /monitor（契约 §5.3）
 * 顶部状态灯/运行时间（M3 轮询 5s）、实时告警流（M17 WS：hello/snapshot/alert/ping/pong/bye + 断线重连）、
 * 自选清单/持仓/规则管理、告警历史分页过滤；各区块 [导出JSON]。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import { useMonitorStore } from '../stores/monitor';
import { useAppStore } from '../stores/app';
import { api } from '../api';
import type { ExportPayload } from '../api/types';
import type { AlertLevel, CriterionType, PositionInput, RuleInput } from '../types';
import type { SecurityHit } from '../types/data';
import { envelopeToBlob } from '../utils/download';
import { fmtDateTime, fmtMoney, fmtNumber, fmtSignedPct } from '../utils/format';
import { toastError, toastSuccess, toastWarning } from '../utils/toast';
import StateBlock from '../components/StateBlock.vue';
import Pagination from '../components/Pagination.vue';
import ExportButton from '../components/ExportButton.vue';
import SecuritySearchBox from '../components/SecuritySearchBox.vue';

const monitor = useMonitorStore();
const app = useAppStore();

type TabKey = 'watchlist' | 'positions' | 'rules' | 'alerts';
const tab = ref<TabKey>('watchlist');

const WS_STATUS_TEXT: Record<string, string> = {
  idle: '未连接',
  connecting: '连接中…',
  open: '已连接',
  reconnecting: '重连中…',
  closed: '已断开',
};

/* ---------- 自选清单 ---------- */
const watchCodes = ref('');

async function addWatchlist(): Promise<void> {
  const codes = watchCodes.value
    .split(/[,，\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (codes.length === 0) {
    toastWarning('请输入证券代码（6 位数字，逗号分隔）');
    return;
  }
  try {
    const result = await monitor.addWatchlist(codes);
    toastSuccess(`已添加 ${result.added.length} 个；跳过 ${result.skipped.length} 个`);
    watchCodes.value = '';
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

async function removeWatchlist(code: string): Promise<void> {
  try {
    await monitor.removeWatchlist(code);
    toastSuccess(`已移除自选 ${code}`);
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

/** 搜索选中 → 追加到输入框并立即添加（选中后填入 code） */
function onWatchSearchSelect(hit: SecurityHit): void {
  const existing = watchCodes.value
    .split(/[,，\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (!existing.includes(hit.code)) {
    existing.push(hit.code);
    watchCodes.value = existing.join(',');
  }
  void addWatchlist();
}

/* ---------- 持仓 ---------- */
const posCode = ref('');
const posShares = ref(100);
const posCost = ref(10);

async function addPosition(): Promise<void> {
  if (!/^\d{6}$/.test(posCode.value.trim())) {
    toastWarning('请输入 6 位证券代码');
    return;
  }
  const input: PositionInput = {
    code: posCode.value.trim(),
    market: 'CN',
    shares: Number(posShares.value),
    cost_price: Number(posCost.value),
    opened_at: null,
  };
  if (!(input.shares > 0) || !(input.cost_price > 0)) {
    toastWarning('数量与成本价必须为正数');
    return;
  }
  try {
    await monitor.addPosition(input);
    toastSuccess(`已添加持仓 ${input.code}`);
    posCode.value = '';
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

async function removePosition(code: string): Promise<void> {
  try {
    await monitor.removePosition(code);
    toastSuccess(`已移除持仓 ${code}`);
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

/* ---------- 规则 ---------- */
const ruleForm = reactive({
  name: '',
  criterion_type: 'price' as CriterionType,
  codes: '',
  level: 'warning' as AlertLevel,
  enabled: true,
  priceOp: 'above',
  priceThreshold: 1600,
  indIndicator: 'ma',
  indOp: 'cross_above',
  indValue: 70,
  indPeriod: 14,
  slBase: 'cost',
  slPct: 0.08,
});

function resetRuleForm(): void {
  ruleForm.name = '';
  ruleForm.criterion_type = 'price';
  ruleForm.codes = '';
  ruleForm.level = 'warning';
  ruleForm.enabled = true;
  ruleForm.priceOp = 'above';
  ruleForm.priceThreshold = 1600;
  ruleForm.indIndicator = 'ma';
  ruleForm.indOp = 'cross_above';
  ruleForm.indValue = 70;
  ruleForm.indPeriod = 14;
  ruleForm.slBase = 'cost';
  ruleForm.slPct = 0.08;
}

async function submitRule(): Promise<void> {
  let params: Record<string, unknown>;
  if (ruleForm.criterion_type === 'price') {
    params = { op: ruleForm.priceOp, threshold: Number(ruleForm.priceThreshold) };
  } else if (ruleForm.criterion_type === 'indicator') {
    params = {
      indicator: ruleForm.indIndicator,
      op: ruleForm.indOp,
      value: Number(ruleForm.indValue),
      period: Number(ruleForm.indPeriod),
    };
  } else {
    params = { base: ruleForm.slBase, pct: Number(ruleForm.slPct) };
  }
  const input: RuleInput = {
    name: ruleForm.name.trim() || '未命名规则',
    scope: {
      market: 'CN',
      codes: ruleForm.codes.trim() ? ruleForm.codes.split(/[,，\s]+/).filter(Boolean) : ['*'],
    },
    criterion_type: ruleForm.criterion_type,
    params,
    level: ruleForm.level,
    enabled: ruleForm.enabled,
    cooldown_seconds: 300,
  };
  try {
    await monitor.addRule(input);
    toastSuccess('规则已创建');
    resetRuleForm();
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

async function toggleRule(id: string, enabled: boolean): Promise<void> {
  try {
    await monitor.setRuleEnabled(id, enabled);
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

async function removeRule(id: string): Promise<void> {
  try {
    await monitor.removeRule(id);
    toastSuccess('规则已删除');
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

/* ---------- 告警历史 ---------- */
const alertLevel = ref('');
const alertPage = ref(1);
const ALERT_PAGE_SIZE = 20;

async function changeAlertLevel(): Promise<void> {
  alertPage.value = 1;
  await monitor.loadAlerts(1, ALERT_PAGE_SIZE, alertLevel.value || undefined);
}

async function goAlertPage(page: number): Promise<void> {
  alertPage.value = page;
  await monitor.loadAlerts(page, ALERT_PAGE_SIZE, alertLevel.value || undefined);
}

/* ---------- P1-2 自选/持仓/规则翻页（后端下推 SQLite LIMIT/OFFSET 后配套） ---------- */
// 分页大小与 store 默认值（100）保持一致；翻页时直接读取 store 的 pageSize（state 已保存）。
async function goWatchlistPage(page: number): Promise<void> {
  await monitor.loadWatchlist(page, monitor.watchlistPageSize);
}

async function goPositionsPage(page: number): Promise<void> {
  await monitor.loadPositions(page, monitor.positionsPageSize);
}

async function goRulesPage(page: number): Promise<void> {
  await monitor.loadRules(page, monitor.rulesPageSize);
}

/* ---------- 监控启停 ---------- */
/** 收集可监控代码：自选 ∪ 启用规则中明确的代码（不含 "*" 全量通配） */
function collectMonitorCodes(): string[] {
  const watch = monitor.watchlist?.items.map((w) => w.code) ?? [];
  const fromRules: string[] = [];
  for (const r of monitor.rules?.items ?? []) {
    if (!r.enabled) continue;
    for (const c of r.scope.codes) {
      if (c !== '*' && !fromRules.includes(c) && !watch.includes(c)) fromRules.push(c);
    }
  }
  return [...watch, ...fromRules];
}

async function toggleMonitor(): Promise<void> {
  try {
    if (monitor.status?.running) {
      await monitor.stopMonitor();
      toastSuccess('监控已停止');
      return;
    }
    // 启动前引导：自选为空时，尝试从启用规则自动补自选；仍无标的则友好提示，
    // 避免把后端的 422 技术错误直接抛给用户。
    const watchCodes = monitor.watchlist?.items.map((w) => w.code) ?? [];
    if (watchCodes.length === 0) {
      const fromRules = collectMonitorCodes().filter((c) => !watchCodes.includes(c));
      if (fromRules.length > 0) {
        await monitor.addWatchlist(fromRules);
        toastWarning(`已从启用规则自动补自选 ${fromRules.length} 个：${fromRules.join(', ')}`);
      } else {
        tab.value = 'watchlist';
        toastWarning('启动监控需至少有一个自选标的（或启用含具体代码的预警规则），请先添加自选');
        return;
      }
    }
    await monitor.startMonitor();
    toastSuccess('监控已启动');
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

/* ---------- 导出 ---------- */
/** P1-2：导出用 500/页（后端路由 page_size 上限）。若以后真有>500条的自选/持仓/规则，
 *  导出需要分页循环；目前量级场景下，一页足以覆盖全量。 */
const EXPORT_PAGE_SIZE = 500;

function exportWatchlist(): Promise<ExportPayload> {
  return api.getWatchlist('CN', 1, EXPORT_PAGE_SIZE).then((env) => ({
    blob: envelopeToBlob(env),
    filename: 'monitor_watchlist.json',
  }));
}

function exportPositions(): Promise<ExportPayload> {
  return api.getPositions('CN', 1, EXPORT_PAGE_SIZE).then((env) => ({
    blob: envelopeToBlob(env),
    filename: 'monitor_positions.json',
  }));
}

function exportAlerts(): Promise<ExportPayload> {
  return api.getAlerts({ market: 'CN', level: (alertLevel.value || undefined) as AlertLevel | undefined, page: 1, pageSize: EXPORT_PAGE_SIZE }).then((env) => ({
    blob: envelopeToBlob(env),
    filename: 'monitor_alerts.json',
  }));
}

/* ---------- 生命周期 ---------- */
onMounted(() => {
  void monitor.init();
});

onBeforeUnmount(() => {
  monitor.stopPolling();
  monitor.disconnectWs();
});

/* ---------- 视图计算 ---------- */
const healthLabel = computed(() => {
  const n = monitor.status?.consecutive_errors ?? 0;
  if (n >= 3) return `连续失败 ${n} 次（红）`;
  if (n > 0) return `连续失败 ${n} 次（黄）`;
  return '轮询健康';
});
</script>

<template>
  <div class="page">
    <!-- 顶部状态灯 -->
    <section class="panel monitor-status-bar">
      <div class="status-item">
        <span class="status-dot" :class="monitor.status?.running ? 'dot-computed' : 'dot-uncomputed'"></span>
        <span>{{ monitor.status?.running ? '监控运行中' : '监控已停止' }}</span>
        <button class="btn btn-sm" :class="monitor.status?.running ? 'btn-danger' : 'btn-primary'" @click="toggleMonitor">
          {{ monitor.status?.running ? '停止' : '启动' }}
        </button>
      </div>
      <div class="status-item" :title="monitor.statusError || ''">
        健康度
        <span
          class="status-dot"
          :class="{ 'dot-computed': monitor.healthTone === 'green', 'text-warn': monitor.healthTone === 'yellow', 'dot-failed': monitor.healthTone === 'red' }"
        ></span>
        <span class="text-secondary">{{ healthLabel }}</span>
      </div>
      <div class="status-item" v-if="monitor.status">
        轮询间隔 <b>{{ monitor.status.poll_interval_seconds }}s</b>
      </div>
      <div class="status-item" v-if="monitor.status">
        交易时段 <b :class="{ 'text-green': monitor.status.in_trading_session }">{{ monitor.status.in_trading_session ? '是' : '否' }}</b>
      </div>
      <div class="status-item" v-if="monitor.status">
        自选 <b>{{ monitor.status.watchlist_count }}</b> · 启用规则 <b>{{ monitor.status.rules_enabled_count }}</b>
      </div>
      <div class="status-item channels" v-if="monitor.channels.length">
        <span v-for="c in monitor.channels" :key="c.name" class="badge" :class="c.enabled ? 'badge-success' : 'badge-cancelled'" :title="c.display_name">
          {{ c.display_name }}{{ c.enabled ? (c.healthy === false ? ' ✗' : ' ✓') : '' }}
        </span>
      </div>
    </section>

    <div class="monitor-grid">
      <!-- 实时告警流（WS） -->
      <section class="panel stream-panel">
        <div class="panel-title">
          实时告警流
          <span class="badge" :class="monitor.wsStatus === 'open' ? 'badge-success' : monitor.wsStatus === 'reconnecting' ? 'badge-warning' : 'badge-cancelled'">
            {{ WS_STATUS_TEXT[monitor.wsStatus] }}
          </span>
          <span v-if="monitor.wsDetail" class="panel-subtitle" :title="monitor.wsDetail">{{ monitor.wsDetail }}</span>
          <button class="btn btn-ghost btn-xs" @click="monitor.reconnectWs()">重连</button>
        </div>
        <div class="alert-list">
          <div v-for="a in monitor.liveAlerts" :key="a.id" class="alert-item" :class="`level-${a.level}`">
            <span class="level-badge">{{ a.level }}</span>
            <span class="alert-code">{{ a.code }}</span>
            <span class="alert-msg" :title="a.message">{{ a.message }}</span>
            <span class="alert-rule" :title="a.rule">{{ a.rule }}</span>
            <span class="alert-ts">{{ fmtDateTime(a.ts) }}</span>
          </div>
          <StateBlock v-if="monitor.liveAlerts.length === 0" state="empty" message="等待告警推送…（WS 连接后可实时接收）" />
        </div>
      </section>

      <!-- 右侧页签 -->
      <section class="panel monitor-right">
        <div class="tabs">
          <button class="tab-btn" :class="{ active: tab === 'watchlist' }" @click="tab = 'watchlist'">自选清单</button>
          <button class="tab-btn" :class="{ active: tab === 'positions' }" @click="tab = 'positions'">持仓</button>
          <button class="tab-btn" :class="{ active: tab === 'rules' }" @click="tab = 'rules'">预警规则</button>
          <button class="tab-btn" :class="{ active: tab === 'alerts' }" @click="tab = 'alerts'">告警历史</button>
        </div>

        <!-- 自选清单 -->
        <div v-if="tab === 'watchlist'" class="tab-pane">
          <div class="pane-toolbar">
            <SecuritySearchBox
              class="wl-search"
              :market="app.market"
              placeholder="搜索代码/名称（如 600000 / 浦发），或直接输入 6 位代码"
              @select="onWatchSearchSelect"
            />
            <input v-model="watchCodes" class="input" type="text" placeholder="或 600519,000858（逗号分隔）" @keyup.enter="addWatchlist" />
            <button class="btn btn-primary btn-sm" @click="addWatchlist">添加</button>
            <ExportButton :fetcher="exportWatchlist" label="导出JSON" />
          </div>
          <StateBlock v-if="!monitor.watchlist" state="empty" message="自选为空" />
          <div v-else class="table-wrap">
            <table class="tbl">
              <thead>
                <tr>
                  <th>代码</th>
                  <th>名称</th>
                  <th>市场</th>
                  <th>来源</th>
                  <th>加入时间</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="w in monitor.watchlist.items" :key="w.code">
                  <td class="mono-cell">{{ w.code }}</td>
                  <td>{{ w.name }}</td>
                  <td>{{ w.market }}</td>
                  <td>{{ w.source }}</td>
                  <td>{{ fmtDateTime(w.added_at) }}</td>
                  <td><button class="btn btn-danger btn-xs" @click="removeWatchlist(w.code)">移除</button></td>
                </tr>
              </tbody>
            </table>
            <!-- P1-2：自选列表分页（原实现 pageSize=100 且无翻页控件，>100 条时后面记录看不到） -->
            <Pagination
              v-if="monitor.watchlist && monitor.watchlist.total > monitor.watchlistPageSize"
              :page="monitor.watchlist.page"
              :page-size="monitor.watchlist.page_size"
              :total="monitor.watchlist.total"
              :total-pages="monitor.watchlist.total_pages"
              @change="goWatchlistPage"
            />
          </div>
        </div>

        <!-- 持仓 -->
        <div v-if="tab === 'positions'" class="tab-pane">
          <div class="pane-toolbar">
            <input v-model="posCode" class="input pos-code" type="text" placeholder="600519" />
            <input v-model.number="posShares" class="input pos-num" type="number" min="1" placeholder="数量(股)" />
            <input v-model.number="posCost" class="input pos-num" type="number" min="0" step="0.01" placeholder="成本价" />
            <button class="btn btn-primary btn-sm" @click="addPosition">添加</button>
            <ExportButton :fetcher="exportPositions" label="导出JSON" />
          </div>
          <StateBlock v-if="!monitor.positions" state="empty" message="暂无持仓" />
          <div v-else class="table-wrap">
            <table class="tbl">
              <thead>
                <tr>
                  <th>代码</th>
                  <th>名称</th>
                  <th class="num">数量</th>
                  <th class="num">成本价</th>
                  <th class="num">最新价</th>
                  <th class="num">当日涨跌</th>
                  <th class="num">市值</th>
                  <th class="num">浮动盈亏</th>
                  <th class="num">盈亏比</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="p in monitor.positions.items" :key="p.code">
                  <td class="mono-cell">{{ p.code }}</td>
                  <td>{{ p.name }}</td>
                  <td class="num">{{ p.shares.toLocaleString('zh-CN') }}</td>
                  <td class="num">{{ fmtNumber(p.cost_price) }}</td>
                  <td class="num">{{ fmtNumber(p.last) }}</td>
                  <td class="num" :class="p.change_pct >= 0 ? 'text-green' : 'text-red'">{{ fmtSignedPct(p.change_pct) }}</td>
                  <td class="num">{{ fmtMoney(p.market_value) }}</td>
                  <td class="num" :class="p.pnl >= 0 ? 'text-green' : 'text-red'">{{ fmtMoney(p.pnl) }}</td>
                  <td class="num" :class="p.pnl_pct >= 0 ? 'text-green' : 'text-red'">{{ fmtSignedPct(p.pnl_pct) }}</td>
                  <td><button class="btn btn-danger btn-xs" @click="removePosition(p.code)">移除</button></td>
                </tr>
              </tbody>
            </table>
            <!-- P1-2：持仓列表分页 -->
            <Pagination
              v-if="monitor.positions && monitor.positions.total > monitor.positionsPageSize"
              :page="monitor.positions.page"
              :page-size="monitor.positions.page_size"
              :total="monitor.positions.total"
              :total-pages="monitor.positions.total_pages"
              @change="goPositionsPage"
            />
          </div>
        </div>

        <!-- 预警规则 -->
        <div v-if="tab === 'rules'" class="tab-pane">
          <!-- 预设监控规则：开箱即用，一键开关 -->
          <section class="preset-section">
            <div class="preset-head">
              <h3 class="preset-title">预设监控规则</h3>
              <span class="preset-hint">开箱即用 · 默认开启 · 可一键关闭</span>
            </div>
            <div v-if="!monitor.presets" class="preset-loading">加载中…</div>
            <div v-else class="preset-grid">
              <div
                v-for="p in monitor.presets"
                :key="p.key"
                class="preset-card"
                :class="{ 'is-off': p.enabled === false }"
              >
                <div class="preset-card-top">
                  <span class="badge" :class="'lv-' + p.level">{{ p.level }}</span>
                  <span
                    class="preset-state"
                    :class="p.enabled ? 'on' : 'off'"
                  >{{ p.enabled ? '已开启' : (p.applied ? '已关闭' : '未启用') }}</span>
                </div>
                <div class="preset-name">{{ p.name }}</div>
                <div class="preset-desc">{{ p.description }}</div>
                <label class="preset-switch">
                  <input
                    type="checkbox"
                    :checked="p.enabled === true"
                    :disabled="monitor.presetsLoading"
                    @change="monitor.togglePreset(p.key)"
                  />
                  <span class="preset-slider"></span>
                  <span class="preset-switch-label">{{ p.enabled ? '开' : '关' }}</span>
                </label>
              </div>
            </div>
          </section>

          <div class="rule-form panel-inner">
            <div class="form-row">
              <div class="field">
                <label>规则名</label>
                <input v-model="ruleForm.name" class="input" type="text" placeholder="如 止损-成本-8%" />
              </div>
              <div class="field">
                <label>判据类型（契约 §3.5）</label>
                <select v-model="ruleForm.criterion_type" class="select">
                  <option value="price">价格阈值</option>
                  <option value="indicator">技术指标</option>
                  <option value="stop_loss">回撤止损</option>
                </select>
              </div>
              <div class="field">
                <label>级别</label>
                <select v-model="ruleForm.level" class="select">
                  <option value="info">info</option>
                  <option value="warning">warning</option>
                  <option value="critical">critical</option>
                </select>
              </div>
              <div class="field">
                <label>范围代码（留空=全部 *）</label>
                <input v-model="ruleForm.codes" class="input" type="text" placeholder="600519,000858" />
              </div>
            </div>

            <div v-if="ruleForm.criterion_type === 'price'" class="form-row">
              <div class="field">
                <label>方向</label>
                <select v-model="ruleForm.priceOp" class="select">
                  <option value="above">上破 above</option>
                  <option value="below">下破 below</option>
                </select>
              </div>
              <div class="field">
                <label>阈值（元）</label>
                <input v-model.number="ruleForm.priceThreshold" class="input" type="number" step="0.01" />
              </div>
            </div>

            <div v-else-if="ruleForm.criterion_type === 'indicator'" class="form-row">
              <div class="field">
                <label>指标</label>
                <select v-model="ruleForm.indIndicator" class="select">
                  <option value="ma">MA</option>
                  <option value="macd">MACD</option>
                  <option value="rsi">RSI</option>
                </select>
              </div>
              <div class="field">
                <label>条件</label>
                <select v-model="ruleForm.indOp" class="select">
                  <option value="cross_above">金叉</option>
                  <option value="cross_below">死叉</option>
                  <option value="gt">&gt;</option>
                  <option value="lt">&lt;</option>
                </select>
              </div>
              <div class="field">
                <label>数值</label>
                <input v-model.number="ruleForm.indValue" class="input" type="number" step="0.1" />
              </div>
              <div class="field">
                <label>周期</label>
                <input v-model.number="ruleForm.indPeriod" class="input" type="number" min="1" />
              </div>
            </div>

            <div v-else class="form-row">
              <div class="field">
                <label>基准</label>
                <select v-model="ruleForm.slBase" class="select">
                  <option value="cost">成本价</option>
                  <option value="peak">区间最高价</option>
                </select>
              </div>
              <div class="field">
                <label>回撤比例（小数，0.08=8%）</label>
                <input v-model.number="ruleForm.slPct" class="input" type="number" step="0.01" min="0" max="1" />
              </div>
            </div>

            <div class="form-row form-row-end">
              <label class="checkbox-label">
                <input v-model="ruleForm.enabled" type="checkbox" /> 启用
              </label>
              <button class="btn btn-primary btn-sm" @click="submitRule">新建规则</button>
            </div>
          </div>

          <StateBlock v-if="!monitor.rules" state="empty" message="暂无规则" />
          <div v-else class="table-wrap">
            <table class="tbl">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>类型</th>
                  <th>范围</th>
                  <th>参数</th>
                  <th>级别</th>
                  <th>启用</th>
                  <th>最近触发</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in monitor.rules.items" :key="r.id">
                  <td>{{ r.name }}</td>
                  <td>{{ r.criterion_type }}</td>
                  <td class="mono-cell">{{ r.scope.codes.join(',') }}</td>
                  <td class="mono-cell param-cell" :title="JSON.stringify(r.params)">{{ JSON.stringify(r.params) }}</td>
                  <td><span class="badge" :class="`level-${r.level}`">{{ r.level }}</span></td>
                  <td>
                    <button class="btn btn-xs" :class="r.enabled ? 'btn-primary' : 'btn-ghost'" @click="toggleRule(r.id, !r.enabled)">
                      {{ r.enabled ? '开' : '关' }}
                    </button>
                  </td>
                  <td>{{ fmtDateTime(r.last_triggered_at) }}</td>
                  <td><button class="btn btn-danger btn-xs" @click="removeRule(r.id)">删除</button></td>
                </tr>
              </tbody>
            </table>
            <!-- P1-2：规则列表分页（>100 条规则场景非常罕见，但为了与后端 DB 下推对齐，保留分页控件） -->
            <Pagination
              v-if="monitor.rules && monitor.rules.total > monitor.rulesPageSize"
              :page="monitor.rules.page"
              :page-size="monitor.rules.page_size"
              :total="monitor.rules.total"
              :total-pages="monitor.rules.total_pages"
              @change="goRulesPage"
            />
          </div>
        </div>

        <!-- 告警历史 -->
        <div v-if="tab === 'alerts'" class="tab-pane">
          <div class="pane-toolbar">
            <select v-model="alertLevel" class="select" @change="changeAlertLevel">
              <option value="">全部级别</option>
              <option value="info">info</option>
              <option value="warning">warning</option>
              <option value="critical">critical</option>
            </select>
            <ExportButton :fetcher="exportAlerts" label="导出JSON" />
          </div>
          <StateBlock v-if="!monitor.alerts" state="empty" message="暂无告警" />
          <template v-else>
            <div class="alert-list alert-history">
              <div v-for="a in monitor.alerts.items" :key="a.id" class="alert-item" :class="`level-${a.level}`">
                <span class="level-badge">{{ a.level }}</span>
                <span class="alert-code">{{ a.code }}</span>
                <span class="alert-msg" :title="a.message">{{ a.message }}</span>
                <span class="alert-rule" :title="a.rule">{{ a.rule }}</span>
                <span class="alert-ts">{{ fmtDateTime(a.ts) }}</span>
              </div>
            </div>
            <Pagination
              :page="monitor.alerts.page"
              :page-size="monitor.alerts.page_size"
              :total="monitor.alerts.total"
              :total-pages="monitor.alerts.total_pages"
              @change="goAlertPage"
            />
          </template>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.monitor-status-bar {
  display: flex;
  align-items: center;
  gap: 22px;
  flex-wrap: wrap;
  padding: 10px 16px;
  font-size: 13px;
}

.status-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.status-item b {
  font-weight: 600;
}

.channels {
  margin-left: auto;
  gap: 4px;
}

.monitor-grid {
  display: grid;
  grid-template-columns: 5fr 7fr;
  gap: 16px;
  align-items: start;
}

.stream-panel {
  position: sticky;
  top: 118px;
}

.monitor-right {
  min-width: 0;
}

.tab-pane {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.pane-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.pane-toolbar .input {
  flex: 1;
  min-width: 200px;
}

.wl-search {
  flex: 1.2;
  min-width: 240px;
}

.pos-code {
  width: 110px;
  flex: none;
}

.pos-num {
  width: 110px;
  flex: none;
}

.panel-inner {
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  padding: 10px 12px;
  background: #fafbfd;
}

.form-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: flex-end;
  margin-bottom: 10px;
}

.form-row .field {
  flex: 1;
  min-width: 130px;
}

.form-row-end {
  justify-content: flex-end;
  margin-bottom: 0;
}

.param-cell {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mono-cell {
  font-family: var(--mono);
  font-size: 12px;
}

.alert-history {
  max-height: 460px;
}

@media (max-width: 1100px) {
  .monitor-grid {
    grid-template-columns: 1fr;
  }

  .stream-panel {
    position: static;
  }
}

/* ---------- 预设监控规则卡片 ---------- */
.preset-section {
  margin-bottom: 18px;
}
.preset-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 12px;
}
.preset-title {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
}
.preset-hint {
  font-size: 12px;
  color: var(--muted);
}
.preset-loading {
  font-size: 13px;
  color: var(--muted);
  padding: 12px 0;
}
.preset-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}
.preset-card {
  background: var(--panel-inner, #11161d);
  border: 1px solid var(--border-soft, #2a323c);
  border-radius: 10px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  transition: border-color 0.15s ease, opacity 0.15s ease;
}
.preset-card.is-off {
  opacity: 0.6;
}
.preset-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.preset-state {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
}
.preset-state.on {
  color: #1b8a4b;
  background: rgba(46, 204, 113, 0.14);
}
.preset-state.off {
  color: var(--muted);
  background: rgba(128, 128, 128, 0.14);
}
.preset-name {
  font-size: 14px;
  font-weight: 600;
}
.preset-desc {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
  min-height: 36px;
}
/* 开关控件 */
.preset-switch {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
  margin-top: 2px;
}
.preset-switch input {
  position: absolute;
  opacity: 0;
  width: 0;
  height: 0;
}
.preset-slider {
  position: relative;
  width: 38px;
  height: 20px;
  border-radius: 999px;
  background: #3a434f;
  transition: background 0.18s ease;
  flex: none;
}
.preset-slider::before {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #e6edf3;
  transition: transform 0.18s ease;
}
.preset-switch input:checked + .preset-slider {
  background: var(--accent, #2f81f7);
}
.preset-switch input:checked + .preset-slider::before {
  transform: translateX(18px);
}
.preset-switch input:disabled + .preset-slider {
  opacity: 0.5;
  cursor: not-allowed;
}
.preset-switch-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
}
.preset-switch input:checked ~ .preset-switch-label {
  color: var(--accent, #2f81f7);
}
</style>
