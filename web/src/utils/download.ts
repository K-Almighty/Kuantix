/**
 * 下载工具（契约 §1.10 导出约定）。
 * - JSON 导出：下载信封 JSON 文本。
 * - CSV 导出：后端返回 text/csv; charset=gbk 文件，前端以 Blob 原样下载（保留 GBK 字节）；
 *   mock 模式以 UTF-8+BOM 模拟（浏览器侧无原生 GBK 编码器）。
 */

export function triggerBlobDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function envelopeToBlob(env: unknown): Blob {
  return new Blob([JSON.stringify(env, null, 2)], { type: 'application/json;charset=utf-8' });
}

export function downloadJson(env: unknown, filename: string): void {
  triggerBlobDownload(envelopeToBlob(env), filename);
}
