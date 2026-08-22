<script setup lang="ts">
/**
 * 服务器设置页 /settings（契约 §2.1f E1–E2，P2；**只读**数据源状态，NF-20）。
 *
 * - E1 GET /api/v1/settings/status：数据源状态卡片（数据路径/默认市场/端口/版本）、
 *   known_hosts 只读表格、数据湖覆盖率卡片；
 * - E2 POST /api/v1/settings/test-connection：host/port/kind 表单 → 连通性测试
 *   （**只测不写**：新建连接 ping 即关；连接失败返回 {ok:false} 业务结果）；
 * - 整页**不含任何写操作 UI**：不提供"切换服务器/保存配置"；配置修改引导用户
 *   编辑 Kuantix 自己的 config.toml（只展示路径，不写）。
 */
import { onMounted, ref } from 'vue';
import { api } from '../api';
import type {
  ClientKind,
  SettingsStatus,
  TestConnectionResult,
} from '../types';
import { fmtDate, fmtInt } from '../utils/format';
import { toastError, toastSuccess, toastWarning } from '../utils/toast';
import { envelopeToBlob } from '../utils/download';
import ExportButton from '../components/ExportButton.vue';
import StateBlock from '../components/StateBlock.vue';

/* ---------- E1 数据源状态 ---------- */
const status = ref<SettingsStatus | null>(null);
const statusLoading = ref(true);
const statusError = ref('');

async function loadStatus(): Promise<void> {
  statusLoading.value = true;
  statusError.value = '';
  try {
    const env = await api.getSettingsStatus();
    status.value = env.data;
  } catch (e) {
    statusError.value = e instanceof Error ? e.message : String(e);
  } finally {
    statusLoading.value = false;
  }
}

async function refreshStatus(): Promise<void> {
  try {
    await loadStatus();
    toastSuccess('数据源状态已刷新');
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}

async function exportSettingsStatus(): Promise<{ blob: Blob; filename: string }> {
  const env = await api.getSettingsStatus();
  return { blob: envelopeToBlob(env), filename: `settings_status_${new Date().toISOString().slice(0, 10)}.json` };
}

/* ---------- E2 连通性测试（只测不写） ---------- */
const KIND_OPTIONS: { value: ClientKind; label: string; hint: string }[] = [
  { value: 'std', label: '标准协议', hint: '证券清单（TdxClient）' },
  { value: 'mac', label: 'MAC 行情', hint: 'A 股 K 线/报价（MacClient）' },
  { value: 'mac_ex', label: '扩展行情', hint: '港美股（MacExClient）' },
];

const testKind = ref<ClientKind>('mac');
const testHost = ref('');
const testPort = ref<number>(7709);
const testing = ref(false);
const testResult = ref<TestConnectionResult | null>(null);
const testError = ref('');

async function onTest(): Promise<void> {
  const host = testHost.value.trim();
  if (!host) {
    toastWarning('请填写服务器 host');
    return;
  }
  if (!(testPort.value >= 1 && testPort.value <= 65535)) {
    toastWarning('端口必须在 1–65535 之间');
    return;
  }
  testing.value = true;
  testError.value = '';
  testResult.value = null;
  try {
    const env = await api.testConnection({
      kind: testKind.value,
      host,
      port: Number(testPort.value),
    });
    testResult.value = env.data;
  } catch (e) {
    testError.value = e instanceof Error ? e.message : String(e);
  } finally {
    testing.value = false;
  }
}

function onKindChange(): void {
  // 切换 kind 时同步端口默认值（std/mac=7709，mac_ex=7727），用户仍可改
  testPort.value = testKind.value === 'mac_ex' ? 7727 : 7709;
  testResult.value = null;
  testError.value = '';
}

/* ---------- 只读路径清单（config.toml 数据目录） ---------- */
const pathRows = [
  { key: 'root', label: '数据根目录' },
  { key: 'vipdoc', label: '行情库（vipdoc）' },
  { key: 'factors', label: '因子库' },
  { key: 'db', label: '数据库' },
  { key: 'logs', label: '日志' },
  { key: 'reports', label: '报告' },
  { key: 'exports', label: '导出' },
] as const;

onMounted(() => {
  void loadStatus();
});
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1 class="page-title">服务器设置</h1>
        <p class="panel-subtitle">只读数据源状态 · Kuantix 不提供「切换服务器 / 保存配置」能力（NF-20）</p>
      </div>
      <div class="toolbar">
        <ExportButton :fetcher="exportSettingsStatus" label="导出JSON" />
        <button class="btn btn-ghost btn-sm" :disabled="statusLoading" @click="refreshStatus">
          {{ statusLoading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <!-- 只读声明横幅（不含任何写操作 UI） -->
    <div class="panel read-only-banner">
      <span class="badge badge-warning">只读</span>
      <span>
        Kuantix 为<b>只读展示</b>，不会改写上游 <code>~/.easy_tdx/config.json</code>。
        如需修改数据源 host，请编辑 Kuantix 自己的配置文件
        <code class="mono">{{ status?.config.config_source || 'config.toml' }}</code>
        （<code>[tdx]</code> 段的 mac_hosts / std_hosts / mac_ex_hosts），修改后重启服务生效。
      </span>
    </div>

    <!-- E1 数据源状态 -->
    <template v-if="statusLoading && !status">
      <StateBlock state="loading" message="加载数据源状态…" />
    </template>
    <template v-else-if="statusError && !status">
      <StateBlock state="error" :message="statusError" />
    </template>
    <template v-else-if="status">
      <!-- 数据源状态卡片 -->
      <div class="panel">
        <div class="panel-title">
          数据源状态
          <span class="panel-subtitle">
            版本：Kuantix {{ status.versions.Kuantix }} · 上游 easy-tdx {{ status.versions.upstream_easy_tdx }}
          </span>
        </div>
        <div class="metric-cards">
          <div class="metric-card">
            <div class="metric-label">默认市场</div>
            <div class="metric-value">{{ status.config.default_market }}</div>
            <div class="metric-sub">启用：{{ status.config.enabled_markets.join(' / ') }}</div>
          </div>
          <div class="metric-card">
            <div class="metric-label">行情端口</div>
            <div class="metric-value">{{ status.config.tdx.port }}</div>
            <div class="metric-sub">std / mac 共用</div>
          </div>
          <div class="metric-card">
            <div class="metric-label">扩展端口</div>
            <div class="metric-value">{{ status.config.tdx.ex_port }}</div>
            <div class="metric-sub">mac_ex（港美股）</div>
          </div>
          <div class="metric-card">
            <div class="metric-label">连接超时</div>
            <div class="metric-value">{{ status.config.tdx.timeout_seconds }}s</div>
            <div class="metric-sub">[tdx].timeout_seconds</div>
          </div>
        </div>

        <!-- 数据路径（只展示，不写） -->
        <div class="sub-block">
          <div class="sub-title">数据路径（{{ status.config.config_source }}）</div>
          <table class="tbl">
            <tbody>
              <tr v-for="row in pathRows" :key="row.key">
                <td class="path-label">{{ row.label }}</td>
                <td class="mono">{{ status.config.paths[row.key] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- known_hosts 只读表格 -->
      <div class="panel">
        <div class="panel-title">
          known_hosts（只读）
          <span class="panel-subtitle">
            上游合入：{{ status.known_hosts.known_hosts_merged ? '是' : '否' }}
            · 上游文件{{ status.known_hosts.upstream_available ? '存在' : '不存在' }}
            · 指纹校验：{{ status.known_hosts.upstream_config_untouched ? '通过（未写）' : '异常' }}
          </span>
        </div>
        <p class="hint-text">
          节点清单来自 config.toml 显式配置 + 上游 <code>{{ status.known_hosts.upstream_config_path }}</code>
          的只读合入（Kuantix 从不写回）。本表仅作展示。
        </p>
        <div class="table-wrap">
          <table class="tbl">
            <thead>
              <tr>
                <th>host</th>
                <th>端口</th>
                <th>类型</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, idx) in status.known_hosts.items" :key="`${row.kind}-${row.host}-${row.port}-${idx}`">
                <td class="mono">{{ row.host }}</td>
                <td class="num">{{ row.port }}</td>
                <td>
                  <span class="badge">{{ row.kind }}</span>
                </td>
                <td>
                  <span class="badge badge-success">{{ row.read_only ? '只读展示' : '?' }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 数据湖覆盖率卡片 -->
      <div class="panel">
        <div class="panel-title">
          数据湖覆盖率
          <span class="panel-subtitle">
            最新数据日 {{ fmtDate(status.data.data_date) }}
            · 隔离区 {{ status.data.quarantine_count }}
          </span>
        </div>
        <div class="metric-cards">
          <div class="metric-card">
            <div class="metric-label">标的数</div>
            <div class="metric-value">{{ fmtInt(status.data.coverage.securities) }}</div>
            <div class="metric-sub">securities</div>
          </div>
          <div class="metric-card">
            <div class="metric-label">文件数</div>
            <div class="metric-value">{{ fmtInt(status.data.coverage.files) }}</div>
            <div class="metric-sub">files</div>
          </div>
          <div class="metric-card">
            <div class="metric-label">行情样本</div>
            <div class="metric-value">{{ fmtInt(status.data.coverage.bars) }}</div>
            <div class="metric-sub">bars</div>
          </div>
        </div>
        <p class="hint-text">
          数据同步（全量/增量）请在顶栏「数据湖状态」区操作，本页仅只读展示。
        </p>
      </div>

      <!-- E2 连通性测试（只测不写） -->
      <div class="panel">
        <div class="panel-title">
          连通性测试（只测不写）
          <span class="panel-subtitle">新建连接 ping 即关 · 不调用 from_best_host / 不落盘任何 best host</span>
        </div>
        <div class="test-form">
          <div class="field">
            <label for="test-kind">类型</label>
            <select id="test-kind" v-model="testKind" class="input" @change="onKindChange">
              <option v-for="opt in KIND_OPTIONS" :key="opt.value" :value="opt.value">
                {{ opt.label }}（{{ opt.hint }}）
              </option>
            </select>
          </div>
          <div class="field">
            <label for="test-host">host</label>
            <input id="test-host" v-model="testHost" class="input" placeholder="如 123.60.47.136" spellcheck="false" />
          </div>
          <div class="field">
            <label for="test-port">port</label>
            <input id="test-port" v-model.number="testPort" class="input" type="number" min="1" max="65535" />
          </div>
          <div class="test-actions">
            <button class="btn btn-primary" :disabled="testing" @click="onTest">
              {{ testing ? '测试中…' : '测试' }}
            </button>
          </div>
        </div>

        <!-- 三态结果 -->
        <div v-if="testing" class="test-result test-result-pending">正在连接 {{ testHost }}:{{ testPort }}（2s 超时）…</div>
        <div v-else-if="testError" class="test-result test-result-error">
          请求失败：{{ testError }}
        </div>
        <div v-else-if="testResult" class="test-result" :class="testResult.ok ? 'test-result-ok' : 'test-result-error'">
          <template v-if="testResult.ok">
            ✅ 连接成功 · {{ testResult.kind }} {{ testResult.host }}:{{ testResult.port }}
            · 延迟 {{ testResult.latency_ms }} ms
          </template>
          <template v-else>
            ❌ 连接失败 · {{ testResult.kind }} {{ testResult.host }}:{{ testResult.port }}
            <div class="test-error-detail">{{ testResult.error }}</div>
          </template>
        </div>
        <div v-else class="test-result test-result-idle">尚未测试——仅连通性探测，不写入任何配置。</div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}

.read-only-banner {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  font-size: 13px;
  color: var(--text-secondary);
  border-left: 3px solid var(--warning, #f59e0b);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.sub-block {
  margin-top: 14px;
}

.sub-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.path-label {
  width: 130px;
  color: var(--text-secondary);
  font-size: 12px;
}

.hint-text {
  font-size: 12px;
  color: var(--text-faint);
  margin: 6px 0 10px;
}

.test-form {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 12px;
}

.test-form .field {
  min-width: 160px;
}

.test-actions {
  display: flex;
  gap: 8px;
  padding-bottom: 2px;
}

.test-result {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: var(--radius);
  font-size: 13px;
}

.test-result-ok {
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  color: #166534;
}

.test-result-error {
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #991b1b;
}

.test-result-pending {
  background: #f5f5f4;
  border: 1px solid var(--border);
  color: var(--text-secondary);
}

.test-result-idle {
  background: #fafafa;
  border: 1px dashed var(--border-strong);
  color: var(--text-faint);
}

.test-error-detail {
  margin-top: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  word-break: break-all;
}
</style>
