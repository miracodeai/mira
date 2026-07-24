import { fetchJson, postJson } from "./http"

// Version, setup, and GitHub install/uninstall lifecycle.
export const systemApi = {
  getVersion: () =>
    fetchJson<{ version: string; bot_name: string }>("/api/version"),

  getSetupStatus: () =>
    fetchJson<{ setup_complete: boolean; repo_count: number }>(
      "/api/setup/status"
    ),

  completeSetup: (
    repos: { owner: string; repo: string; platform: string; enabled: boolean }[],
    index_mode: string
  ) =>
    postJson<{ status: string; repos: number }>("/api/setup/complete", {
      repos,
      index_mode,
    }),

  syncRepos: () =>
    postJson<{ synced: number; removed: number }>("/api/repos/sync", {}),

  listPendingUninstalls: () =>
    fetchJson<{ installation_id: number; owner: string }[]>(
      "/api/uninstalls/pending"
    ),

  keepUninstallData: (installation_id: number) =>
    postJson<{ ok: boolean }>(`/api/uninstalls/${installation_id}/keep`, {}),

  deleteUninstallData: (installation_id: number) =>
    postJson<{ ok: boolean; removed: number }>(
      `/api/uninstalls/${installation_id}/delete`,
      {}
    ),
}
