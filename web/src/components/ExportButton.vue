<script setup lang="ts">
/** [导出JSON] / [导出CSV] 按钮（契约 §1.10）：fetcher 返回 Blob 下载产物 */
import { ref } from 'vue';
import type { ExportPayload } from '../api/types';
import { triggerBlobDownload } from '../utils/download';
import { toastError, toastSuccess } from '../utils/toast';

const props = withDefaults(
  defineProps<{
    label?: string;
    fetcher: () => Promise<ExportPayload>;
  }>(),
  { label: '导出JSON' },
);

const loading = ref(false);

async function onClick(): Promise<void> {
  if (loading.value) return;
  loading.value = true;
  try {
    const payload = await props.fetcher();
    triggerBlobDownload(payload.blob, payload.filename);
    toastSuccess(`已导出 ${payload.filename}`);
  } catch (e) {
    // 错误 toast 已由 api 层统一弹出
    toastError(e instanceof Error ? e.message : String(e));
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <button class="btn btn-ghost btn-sm" :disabled="loading" @click="onClick">
    {{ loading ? '导出中…' : label }}
  </button>
</template>
