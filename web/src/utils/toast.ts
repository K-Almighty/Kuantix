/**
 * 轻量 toast 管理器：错误/成功/警告/信息提示。
 * 统一信封 code≠0 的错误由 api 层调用 toastError（交互类请求）。
 */
import { reactive } from 'vue';

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface ToastItem {
  id: number;
  type: ToastType;
  message: string;
}

export const toastState = reactive<{ items: ToastItem[] }>({ items: [] });

let toastSeq = 0;

export function pushToast(type: ToastType, message: string, timeoutMs = 5000): void {
  const id = ++toastSeq;
  toastState.items.push({ id, type, message });
  window.setTimeout(() => {
    const idx = toastState.items.findIndex((t) => t.id === id);
    if (idx >= 0) toastState.items.splice(idx, 1);
  }, timeoutMs);
}

export function toastSuccess(message: string): void {
  pushToast('success', message);
}

export function toastError(message: string): void {
  pushToast('error', message, 6000);
}

export function toastWarning(message: string): void {
  pushToast('warning', message, 6000);
}

export function toastInfo(message: string): void {
  pushToast('info', message, 4000);
}
