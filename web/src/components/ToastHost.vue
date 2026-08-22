<script setup lang="ts">
/** 全局 toast 宿主 */
import { toastState } from '../utils/toast';
</script>

<template>
  <div class="toast-host">
    <TransitionGroup name="toast">
      <div
        v-for="t in toastState.items"
        :key="t.id"
        class="toast-item"
        :class="`toast-${t.type}`"
      >
        <span class="toast-label">{{ t.type }}</span>
        <span class="toast-msg">{{ t.message }}</span>
      </div>
    </TransitionGroup>
  </div>
</template>
