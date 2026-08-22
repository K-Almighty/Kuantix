<script setup lang="ts">
/** 三态组件：加载 / 空 / 错误（加载/空态/错误态） */
withDefaults(
  defineProps<{
    state: 'loading' | 'empty' | 'error';
    message?: string;
  }>(),
  { message: '' },
);
</script>

<template>
  <div class="state-block">
    <template v-if="state === 'loading'">
      <div class="spinner" aria-label="加载中"></div>
      <p class="state-text">加载中…</p>
    </template>
    <template v-else-if="state === 'empty'">
      <div class="state-icon">◌</div>
      <p class="state-text">{{ message || '暂无数据' }}</p>
      <div class="state-block-actions">
        <slot></slot>
      </div>
    </template>
    <template v-else>
      <div class="state-icon state-icon-error">!</div>
      <p class="state-text state-text-error">{{ message || '加载失败' }}</p>
    </template>
  </div>
</template>
