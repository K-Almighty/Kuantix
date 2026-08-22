<script setup lang="ts">
/**
 * 数据湖空态/未迁移引导条（全局挂载于 TopBar 数据湖指示器下方）。
 *
 * 分三场景（依赖 D1 /data/status 的 storage.source，v1.5 新增）：
 * - 场景 A（都空，storage.source==='empty'）→ 引导「data sync 建湖」（现状）；
 * - 场景 B（仅镜像有 / 未迁移，storage.source==='mirror_only'）→ 引导
 *   「data migrate」而不是重新全量拉取 508M；「立即迁移」按钮为 P2
 *   （后端尚无迁移端点，先提示 CLI 命令，按钮禁用标注）；
 * - 场景 C（sqlite / both，正常）→ 不显示任何引导。
 *
 * 兼容旧后端/mock：无 storage.source 时由 store 按 coverage 回退推导，
 * 仅 securities===0 才算场景 A。
 */
import { ref } from 'vue';
import { useAppStore } from '../stores/app';
import { toastError, toastSuccess } from '../utils/toast';
import JobProgress from './JobProgress.vue';

const app = useAppStore();

const SYNC_CMD = 'Kuantix data sync --market CN --years 10';
const MIGRATE_CMD = 'Kuantix data migrate --verify';

const syncBusy = ref(false);

async function doFullSync(): Promise<void> {
  if (syncBusy.value) return;
  syncBusy.value = true;
  try {
    await app.startSync('full');
    toastSuccess('全量同步已触发（可在上方数据湖指示器查看进度）');
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  } finally {
    syncBusy.value = false;
  }
}

async function refreshStatus(): Promise<void> {
  await app.refreshDataStatus();
  if (!app.dataLakeEmpty && !app.dataLakeMirrorOnly) toastSuccess('数据湖状态已更新');
}

async function onCancelSync(): Promise<void> {
  try {
    await app.cancelSync();
  } catch (e) {
    toastError(e instanceof Error ? e.message : String(e));
  }
}
</script>

<template>
  <!-- 场景 A：都空（真未建湖）→ 引导同步建湖 -->
  <div v-if="app.dataLakeEmpty" class="lake-empty">
    <div class="lake-empty-body">
      <span class="lake-empty-icon">▣</span>
      <div class="lake-empty-text">
        <b>数据湖为空</b>：因子计算 / 选股 / 回测依赖本地行情数据，请先同步建湖。
        <code class="lake-cmd">{{ SYNC_CMD }}</code>
        <span class="lake-empty-sub">或在顶部数据湖指示器点击「全量同步」，完成后状态自动更新。</span>
      </div>
      <div class="lake-empty-actions">
        <button class="btn btn-primary btn-sm" :disabled="syncBusy" @click="doFullSync">
          {{ syncBusy ? '触发中…' : '立即全量同步' }}
        </button>
        <button class="btn btn-ghost btn-sm" :disabled="app.dataStatusLoading" @click="refreshStatus">
          刷新状态
        </button>
      </div>
    </div>
    <JobProgress v-if="app.syncJob && (app.syncJob.status === 'queued' || app.syncJob.status === 'running')" :job="app.syncJob" class="lake-job" @cancel="onCancelSync" />
  </div>

  <!-- 场景 B：仅镜像有（未迁移）→ 引导迁移，不重拉 -->
  <div v-else-if="app.dataLakeMirrorOnly" class="lake-empty lake-migrate">
    <div class="lake-empty-body">
      <span class="lake-empty-icon">⇄</span>
      <div class="lake-empty-text">
        <b>检测到已有行情数据（vipdoc 镜像）</b>：无需重新全量同步，请先迁移到 SQLite 主存储。
        <code class="lake-cmd">{{ MIGRATE_CMD }}</code>
        <span class="lake-empty-sub">
          镜像数据已可被因子 / 选股 / 回测读取（auto 后端自动兜底）；迁移后 SQLite 成为主存储。
        </span>
      </div>
      <div class="lake-empty-actions">
        <button class="btn btn-primary btn-sm" :disabled="true" title="P2：后端迁移端点未接入，请使用上方 CLI 命令">
          立即迁移（P2）
        </button>
        <button class="btn btn-ghost btn-sm" :disabled="app.dataStatusLoading" @click="refreshStatus">
          刷新状态
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.lake-empty {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 8px;
  padding: 10px 14px;
  border: 1px solid var(--border-strong);
  border-left: 4px solid #d97706;
  border-radius: var(--radius);
  background: #fffbeb;
}

.lake-migrate {
  border-left-color: #2563eb;
  background: #eff6ff;
}

.lake-migrate .lake-empty-text b {
  color: #1d4ed8;
}

.lake-migrate .lake-cmd {
  background: #dbeafe;
  border-color: #93c5fd;
  color: #1e40af;
}

.lake-empty-body {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.lake-empty-icon {
  font-size: 18px;
  color: #d97706;
}

.lake-migrate .lake-empty-icon {
  color: #2563eb;
}

.lake-empty-text {
  flex: 1;
  min-width: 260px;
  font-size: 13px;
  color: var(--text);
  line-height: 1.6;
}

.lake-empty-text b {
  color: #b45309;
}

.lake-cmd {
  display: inline-block;
  margin: 0 6px;
  padding: 1px 8px;
  font-family: var(--mono);
  font-size: 12px;
  background: #fff7ed;
  border: 1px solid #fdba74;
  border-radius: 4px;
  color: #9a3412;
}

.lake-empty-sub {
  display: block;
  font-size: 12px;
  color: var(--text-secondary);
}

.lake-empty-actions {
  display: flex;
  gap: 8px;
}

.lake-job {
  width: 100%;
}
</style>
