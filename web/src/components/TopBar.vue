<script setup lang="ts">
/** 全局顶栏（契约 §5.1）：
 * - 品牌 + 导航
 * - 市场切换器：CN 可用，HK/US 置灰不可选（P0 仅 CN，禁用态明确）
 * - 后端连接状态（/api/version + /health 探测，端口从配置读取，禁止硬编码）
 * - 数据湖状态指示器（最新数据日期 + 同步按钮 + Job 进度 + 导出JSON）
 */
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { useAppStore } from '../stores/app';
import { api } from '../api';
import SecuritySearchBox from './SecuritySearchBox.vue';
import type { SecurityHit } from '../types/data';
import { toastSuccess, toastError } from '../utils/toast';
import { envelopeToBlob } from '../utils/download';
import JobProgress from './JobProgress.vue';
import ExportButton from './ExportButton.vue';
import DataLakeEmptyHint from './DataLakeEmptyHint.vue';

const app = useAppStore();
const router = useRouter();

/** 顶部搜索选中 → 跳转个股详情页（/stock/:code?name=...） */
function onSearchSelect(hit: SecurityHit): void {
  router.push({ name: 'stock-detail', params: { code: hit.code }, query: { name: hit.name } });
}


const markets = [
  { code: 'CN', label: 'A股' },
  { code: 'HK', label: '港股' },
  { code: 'US', label: '美股' },
];

const syncBusy = ref(false);

async function doSync(mode: 'full' | 'incremental'): Promise<void> {
  if (syncBusy.value) return;
  syncBusy.value = true;
  try {
    await app.startSync(mode);
    toastSuccess(mode === 'full' ? '全量同步已触发' : '增量同步已触发');
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  } finally {
    syncBusy.value = false;
  }
}

async function cancelSync(): Promise<void> {
  try {
    await app.cancelSync();
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

async function exportDataStatus(): Promise<{ blob: Blob; filename: string }> {
  const env = await api.getDataStatus(app.market);
  return { blob: envelopeToBlob(env), filename: `data_status_${app.market}.json` };
}

async function retryProbe(): Promise<void> {
  await app.probe();
  if (app.connected) toastSuccess('后端连接成功');
}
</script>

<template>
  <header class="topbar">
    <div class="topbar-row">
      <div class="brand">
        <span class="brand-mark">Q</span>
        <span class="brand-name">Kuantix 量化研究台</span>
      </div>

      <!-- 个股搜索（D8）：输入代码/名称实时搜索，选中跳转详情页 -->
      <SecuritySearchBox
        :market="app.market"
        placeholder="搜索个股（代码/名称），如 600000 / 浦发"
        @select="onSearchSelect"
      />

      <nav class="nav">
        <RouterLink to="/factors" class="nav-link">因子分析</RouterLink>
        <RouterLink to="/pre-open" class="nav-link">盘前分析</RouterLink>
        <RouterLink to="/post-close" class="nav-link">盘后复盘</RouterLink>
        <RouterLink to="/monitor" class="nav-link">监控看板</RouterLink>
        <RouterLink to="/screen" class="nav-link">选股结果</RouterLink>
        <RouterLink to="/backtest" class="nav-link">选股回测</RouterLink>
        <RouterLink to="/portfolio" class="nav-link">组合回测</RouterLink>
        <RouterLink to="/optimize" class="nav-link">参数寻优</RouterLink>
        <RouterLink to="/compare" class="nav-link">结果对比</RouterLink>
        <RouterLink to="/strategies" class="nav-link">策略库</RouterLink>
      </nav>

      <div class="topbar-right">
        <!-- 市场切换器：HK/US 置灰不可选（P0 仅 CN，契约 §1.8 NF-6） -->
        <div class="market-switcher" role="group" aria-label="市场切换">
          <button
            v-for="m in markets"
            :key="m.code"
            class="market-btn"
            :class="{ active: app.market === m.code }"
            :disabled="m.code !== 'CN'"
            :title="m.code === 'CN' ? '当前市场：A股' : 'P0 未启用（接口先行、拒绝静默降级）'"
            @click="app.setMarket(m.code)"
          >
            {{ m.label }}
          </button>
        </div>

        <!-- 后端连接状态 -->
        <div class="conn" :class="app.connected ? 'conn-ok' : 'conn-bad'">
          <span class="conn-dot"></span>
          <span class="conn-text">
            <template v-if="app.probing">探测中…</template>
            <template v-else-if="app.connected">已连接 v{{ app.version?.version }}</template>
            <template v-else>后端未连接</template>
          </span>
          <button class="btn btn-ghost btn-xs" @click="retryProbe">重试</button>
        </div>
      </div>
    </div>

    <!-- 数据湖状态指示器（§5.1 全局） -->
    <div class="topbar-row topbar-datalake">
      <div class="datalake-info">
        <span class="datalake-item">
          最新数据日 <b>{{ app.latestDataDate }}</b>
        </span>
        <span v-if="app.dataStatus" class="datalake-item">
          标的 <b>{{ app.dataStatus.coverage.securities.toLocaleString('zh-CN') }}</b>
        </span>
        <span v-if="app.dataStatus" class="datalake-item">
          行情样本 <b>{{ app.dataStatus.coverage.bars.toLocaleString('zh-CN') }}</b>
        </span>
        <span v-if="app.dataStatus" class="datalake-item">
          隔离区 <b :class="{ 'text-warn': (app.dataStatus.quarantine_count ?? 0) > 0 }">{{ app.dataStatus.quarantine_count ?? 0 }}</b>
        </span>
        <span v-if="app.inSyncWindow" class="badge badge-warning" title="NF-28：交易时段强制全量回补需显式确认">
          交易时段中
        </span>
        <span v-if="app.dataStatusError" class="datalake-error" :title="app.dataStatusError">数据湖不可用</span>
      </div>
      <div class="datalake-actions">
        <ExportButton :fetcher="exportDataStatus" label="导出JSON" />
        <button class="btn btn-ghost btn-sm" :disabled="syncBusy" @click="doSync('incremental')">增量同步</button>
        <button class="btn btn-ghost btn-sm" :disabled="syncBusy" @click="doSync('full')">全量同步</button>
      </div>
      <JobProgress v-if="app.syncJob" :job="app.syncJob" class="topbar-job" @cancel="cancelSync" />
      <span class="api-base" :title="app.apiBase">{{ app.apiBase }}</span>
    </div>

    <!-- 数据湖空态引导条（未建湖/未同步时全局提示，各页面复用） -->
    <DataLakeEmptyHint />
  </header>
</template>
