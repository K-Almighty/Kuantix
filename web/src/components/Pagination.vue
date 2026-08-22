<script setup lang="ts">
/** 分页组件（契约 §1.6：page 从 1 起） */
const props = defineProps<{
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
}>();

const emit = defineEmits<{ (e: 'change', page: number): void }>();

function pages(): (number | '…')[] {
  const n = props.totalPages;
  if (n <= 7) return Array.from({ length: n }, (_, i) => i + 1);
  const cur = props.page;
  const set = new Set<number>([1, 2, cur - 1, cur, cur + 1, n - 1, n].filter((p) => p >= 1 && p <= n));
  const sorted = [...set].sort((a, b) => a - b);
  const out: (number | '…')[] = [];
  let prev = 0;
  sorted.forEach((p) => {
    if (prev > 0 && p - prev > 1) out.push('…');
    out.push(p);
    prev = p;
  });
  return out;
}

function go(p: number): void {
  if (p < 1 || p > props.totalPages || p === props.page) return;
  emit('change', p);
}
</script>

<template>
  <div class="pagination">
    <span class="pagination-info">
      共 {{ total }} 条 · 第 {{ page }}/{{ totalPages || 1 }} 页（每页 {{ pageSize }}）
    </span>
    <div class="pagination-btns">
      <button class="btn btn-ghost btn-xs" :disabled="page <= 1" @click="go(page - 1)">上一页</button>
      <template v-for="(p, i) in pages()" :key="i">
        <button
          v-if="p !== '…'"
          class="btn btn-xs"
          :class="p === page ? 'btn-primary' : 'btn-ghost'"
          @click="go(p)"
        >
          {{ p }}
        </button>
        <span v-else class="pagination-ellipsis">…</span>
      </template>
      <button class="btn btn-ghost btn-xs" :disabled="page >= totalPages" @click="go(page + 1)">下一页</button>
    </div>
  </div>
</template>
