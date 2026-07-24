import { deleteJson, fetchJson, postJson, putJson } from "./http"

// Admin webhook management: list/create/update/delete plus a test-fire.
export const webhooksApi = {
  getWebhooks: () =>
    fetchJson<{
      webhooks: {
        id: string
        name: string
        url_masked: string
        events: string[]
        enabled: boolean
        format: string
      }[]
      available_events: { value: string; label: string; description: string }[]
    }>("/api/admin/webhooks"),

  // Full webhook incl. the unmasked URL — for populating the edit form.
  getWebhook: (id: string) =>
    fetchJson<{
      id: string
      name: string
      url: string
      events: string[]
      format: string
    }>(`/api/admin/webhooks/${id}`),

  createWebhook: (body: { name: string; url: string; events: string[] }) =>
    postJson<{ id: string }>("/api/admin/webhooks", body),

  updateWebhook: (
    id: string,
    body: { name?: string; url?: string; events?: string[] }
  ) => putJson<{ id: string }>(`/api/admin/webhooks/${id}`, body),

  deleteWebhook: (id: string) => deleteJson(`/api/admin/webhooks/${id}`),

  testWebhook: (id: string) =>
    postJson<{ ok: boolean; detail: string }>(
      `/api/admin/webhooks/${id}/test`,
      {}
    ),
}
