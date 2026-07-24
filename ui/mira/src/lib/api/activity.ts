import { fetchJson } from "./http"
import type { ActivityDetailModel, ActivityResponse } from "./types"

// Org-wide feed of review events across all repos.
export const activityApi = {
  listActivity: (params?: { limit?: number; repo?: string; q?: string }) => {
    const qs = new URLSearchParams()
    if (params?.limit != null) qs.set("limit", String(params.limit))
    if (params?.repo) qs.set("repo", params.repo)
    if (params?.q) qs.set("q", params.q)
    const query = qs.toString()
    return fetchJson<ActivityResponse>(
      query ? `/api/activity?${query}` : "/api/activity"
    )
  },

  // Full detail (reviews + comments + files + human replies) for one PR.
  getActivityDetail: (owner: string, repo: string, prNumber: number) =>
    fetchJson<ActivityDetailModel>(
      `/api/activity/${owner}/${repo}/${prNumber}`
    ),
}
