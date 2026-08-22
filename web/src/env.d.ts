/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 Base URL（契约 §1.1），默认 http://127.0.0.1:8899/api/v1，可经环境变量覆盖 */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
