/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** `demo` (default): static recorded data only. `live`: the REST API as well. */
  readonly VITE_MORPH_MODE?: string
  readonly VITE_MORPH_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
