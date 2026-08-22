<script setup lang="ts">
import { onMounted } from 'vue';
import TopBar from './components/TopBar.vue';
import ToastHost from './components/ToastHost.vue';
import { useAppStore } from './stores/app';

const app = useAppStore();

onMounted(() => {
  void app.init();
});
</script>

<template>
  <div class="app-shell">
    <TopBar />
    <main class="app-main">
      <RouterView />
    </main>
    <ToastHost />
  </div>
</template>
