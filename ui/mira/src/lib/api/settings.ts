import { fetchJson, putJson } from "./http"

// Model selection, cost estimate, and admin review-config overrides.
export const settingsApi = {
  getModels: () =>
    fetchJson<{
      indexing_model: string
      review_model: string
      backend: string
      indexing_source: "dashboard" | "config"
      review_source: "dashboard" | "config"
      config_indexing_model: string
      config_review_model: string
      indexing_options: {
        value: string
        label: string
        recommended?: boolean
      }[]
      review_options: { value: string; label: string; recommended?: boolean }[]
      review_thinking_mode: string
      thinking_options: { value: string; label: string; recommended?: boolean }[]
    }>("/api/settings/models"),

  saveModels: (
    indexing_model: string,
    review_model: string,
    review_thinking_mode: string = "off",
  ) =>
    putJson<{ ok: boolean }>("/api/settings/models", {
      indexing_model,
      review_model,
      review_thinking_mode,
    }),

  getCostEstimate: () =>
    fetchJson<{
      estimated_usd: number
      input_tokens: number
      output_tokens: number
      model: string
      file_count: number
    }>("/api/indexing/estimate"),

  getGlobalSettings: () =>
    fetchJson<{
      overrides: {
        filter?: Record<string, number | boolean | string>
        review?: Record<string, number | boolean | string>
      }
      effective: Record<string, unknown>
    }>("/api/admin/settings"),

  saveGlobalSettings: (
    overrides: Record<string, Record<string, number | boolean | string>>
  ) => putJson<{ ok: boolean }>("/api/admin/settings", { overrides }),
}
