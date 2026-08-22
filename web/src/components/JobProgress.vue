<script setup lang="ts">
/** Job 进度展示（契约 §1.9）：status 生命周期 queued → running → done|failed|cancelled */
import type { Job } from '../types';

const props = withDefaults(
  defineProps<{
    job: Job | null;
    cancelable?: boolean;
  }>(),
  { cancelable: true },
);

const emit = defineEmits<{ (e: 'cancel'): void }>();

const STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

function percent(job: Job | null): number {
  return job?.progress?.percent ?? 0;
}

function isActive(job: Job | null): boolean {
  return job?.status === 'queued' || job?.status === 'running';
}
</script>

<template>
  <div v-if="props.job" class="job-progress panel">
    <div class="job-progress-head">
      <span class="job-module">{{ props.job.module }} / {{ props.job.action }}</span>
      <span class="badge" :class="`badge-${props.job.status}`">{{ STATUS_LABEL[props.job.status] || props.job.status }}</span>
      <span class="job-id" :title="props.job.job_id">{{ props.job.job_id }}</span>
      <button v-if="cancelable && isActive(props.job)" class="btn btn-ghost btn-xs" @click="emit('cancel')">取消</button>
    </div>
    <div v-if="props.job.progress" class="job-bar">
      <div class="progress-track">
        <div class="progress-fill" :style="{ width: percent(props.job) + '%' }"></div>
      </div>
      <span class="progress-label">
        {{ percent(props.job).toFixed(1) }}% · 完成 {{ props.job.progress.done }}/{{ props.job.progress.total }}
        · 失败 {{ props.job.progress.failed }} · 隔离 {{ props.job.progress.quarantined }}
      </span>
      <span v-if="props.job.progress.current" class="progress-current">当前: {{ props.job.progress.current }}</span>
    </div>
    <div v-if="props.job.result_summary" class="job-summary">
      完成摘要：{{ JSON.stringify(props.job.result_summary) }}
    </div>
    <div v-if="props.job.error" class="job-error">错误：{{ props.job.error.message }}</div>
  </div>
</template>
