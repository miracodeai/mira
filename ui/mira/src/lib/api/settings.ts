import { deleteJson, fetchJson, postJson, putJson } from "./http"
import type { ModelOption } from "@/components/model-combobox"

export type LlmAccount = {
  id: string
  email: string
  active: boolean
  rate_limited: boolean
}
export type UsageWindow = {
  label: string
  percent: number
  resets_at: string | null
}
export type LlmAccountProvider = "chatgpt" | "anthropic"
export type LlmAccounts = {
  accounts: Record<LlmAccountProvider, LlmAccount[]>
}
export type ProviderPreset = {
  id: string
  label: string
  tier: "free" | "paid" | "local"
  base_url: string
  dashboard: string
  note: string
  key: "required" | "optional" | "none"
}
export type ConnectedProvider = {
  id: string
  label: string
  base_url: string
  has_key: boolean
}

// Model selection, cost estimate, and admin review-config overrides.
export const settingsApi = {
  getLlmAccounts: () => fetchJson<LlmAccounts>("/api/llm-accounts"),
  getLlmUsage: () =>
    fetchJson<{
      usage: Record<LlmAccountProvider, Record<string, UsageWindow[]>>
    }>("/api/llm-accounts/usage"),
  startLlmLogin: (provider: LlmAccountProvider) =>
    postJson<{ login_id: string; url: string; code?: string }>(
      `/api/llm-accounts/${provider}/login`,
      {}
    ),
  getLlmLogin: (loginId: string) =>
    fetchJson<{ status: string; error?: string | null }>(
      `/api/llm-accounts/logins/${encodeURIComponent(loginId)}`
    ),
  completeClaudeLogin: (login_id: string, code: string) =>
    postJson<{ ok: boolean }>("/api/llm-accounts/anthropic/complete", {
      login_id,
      code,
    }),
  activateLlmAccount: (provider: LlmAccountProvider, id: string) =>
    postJson<{ ok: boolean }>(
      `/api/llm-accounts/${provider}/${id}/activate`,
      {}
    ),
  removeLlmAccount: (provider: LlmAccountProvider, id: string) =>
    deleteJson(`/api/llm-accounts/${provider}/${id}`),
  logOutLlmProvider: (provider: LlmAccountProvider) =>
    deleteJson(`/api/llm-accounts/${provider}`),
  getApiProviders: () =>
    fetchJson<{ presets: ProviderPreset[]; connected: ConnectedProvider[] }>(
      "/api/llm-providers"
    ),
  connectApiProvider: (body: {
    preset_id?: string
    label?: string
    base_url: string
    api_key: string
  }) =>
    postJson<{ ok: boolean; id: string; models: number }>(
      "/api/llm-providers",
      body
    ),
  disconnectApiProvider: (id: string) =>
    deleteJson(`/api/llm-providers/${encodeURIComponent(id)}`),

  getModels: () =>
    fetchJson<{
      indexing_model: string
      review_model: string
      security_model: string
      backend: string
      indexing_source: "dashboard" | "config"
      review_source: "dashboard" | "config"
      security_source: "dashboard" | "config"
      config_indexing_model: string
      config_review_model: string
      config_security_model: string
      security_inherits_review: boolean
      indexing_options: ModelOption[]
      review_options: ModelOption[]
      security_options: ModelOption[]
      review_thinking_mode: string
      indexing_reasoning: string
      security_reasoning: string
      critique_reasoning: string
      reasoning_overrides: Record<string, string>
      thinking_options: {
        value: string
        label: string
        recommended?: boolean
      }[]
      api_style: string
      api_style_options: {
        value: string
        label: string
        recommended?: boolean
      }[]
      indexing_fallbacks: string[]
      review_fallbacks: string[]
      security_fallbacks: string[]
      missing_api_key: string
      critique_model: string
      ensemble_models: string[]
    }>("/api/settings/models"),

  saveModels: (
    indexing_model: string,
    review_model: string,
    security_model: string,
    review_thinking_mode: string = "",
    api_style: string = "chat",
    extra?: {
      indexing_fallbacks: string[]
      review_fallbacks: string[]
      security_fallbacks: string[]
      critique_model: string
      ensemble_models: string[]
      indexing_reasoning: string
      security_reasoning: string
      critique_reasoning: string
    }
  ) =>
    putJson<{ ok: boolean }>("/api/settings/models", {
      indexing_model,
      review_model,
      security_model,
      review_thinking_mode,
      api_style,
      ...extra,
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
